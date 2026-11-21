"""Risk forecasting, risk budgeting, and walk-forward backtest strategies."""

from __future__ import annotations

from dataclasses import dataclass, field
import warnings

import numpy as np
import pandas as pd
from tqdm import tqdm
from ouput.paths import configure_plotting


@dataclass(frozen=True)
class PipelineConfig:
    start_date: str = "2022-08-01"
    end_date: str = field(
        default_factory=lambda: (
            pd.Timestamp.now(tz="America/New_York") - pd.offsets.BDay(1)
        ).strftime("%Y-%m-%d")
    )
    lookback_window: int = 540
    rebalance_freq: int = 21
    transaction_cost_rate: float = 0.001
    garch_horizon: int = 21
    safe_target: dict[str, float] = field(
        default_factory=lambda: {
            "BIL": 0.05,
            # "GLD": 0.05,
        }
    )
    risk_budget: dict[str, float] = field(
        default_factory=lambda: {
            "VOO": 0.25,
            "QQQ": 0.5,
            "VXUS": 0.25,
        }
    )
    verbose: bool = False

    def __post_init__(self):
        object.__setattr__(self, "safe_target", {k: v for k, v in self.safe_target.items() if v > 0})
        object.__setattr__(self, "risk_budget", {k: v for k, v in self.risk_budget.items() if v > 0})

    @property
    def safe_assets(self) -> list[str]:
        return list(self.safe_target.keys())

    @property
    def tickers(self) -> list[str]:
        return list(self.risk_budget.keys()) + self.safe_assets

    @property
    def risk_assets(self) -> list[str]:
        return list(self.risk_budget.keys())


CFG = PipelineConfig()


def clean_returns(df: pd.DataFrame) -> pd.DataFrame:
    """Forward-fill gaps and drop rows that are still invalid.

    Backward fill is intentionally avoided: it would leak future returns
    into past observations and introduce look-ahead bias in the backtest.
    """
    out = df.replace([np.inf, -np.inf], np.nan).ffill()
    return out.dropna(axis=0, how="any")


def forecast_garch_volatility(returns: pd.DataFrame, horizon: int = 21) -> pd.Series:
    """Forecast annualized volatility per asset via GARCH(1,1)-t."""
    configure_plotting()
    from arch import arch_model
    from arch.utility.exceptions import ConvergenceWarning

    vols: dict[str, float] = {}

    for asset in returns.columns:
        r = returns[asset]
        scaled = r * 100.0

        fallback = float(max(r.std() * np.sqrt(252), 1e-6))
        if scaled.std() < 1e-8:
            vols[asset] = fallback
            continue

        model = arch_model(scaled, mean="Constant", vol="GARCH", p=1, q=1, dist="t")
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", ConvergenceWarning)
                res = model.fit(disp="off")

            if getattr(res, "convergence_flag", 0) != 0:
                vols[asset] = fallback
                continue

            forecast = res.forecast(horizon=horizon, reindex=False)
            avg_var = forecast.variance.values[-1].mean()
            ann_vol = np.sqrt((avg_var / (100.0**2)) * 252.0)
            vols[asset] = float(max(ann_vol, 1e-6))
        except Exception:
            vols[asset] = fallback

    return pd.Series(vols)


def build_forecast_covariance(
    returns: pd.DataFrame, forecasted_vols: pd.Series
) -> pd.DataFrame:
    """Blend forward-looking GARCH covariance with Ledoit-Wolf shrinkage."""
    configure_plotting()
    from pypfopt.risk_models import CovarianceShrinkage

    corr = returns.corr()
    vol_vec = forecasted_vols.reindex(returns.columns).fillna(1e-6).to_numpy(dtype=float)
    d = np.diag(vol_vec)

    cov_garch = pd.DataFrame(
        d @ corr.values @ d,
        index=returns.columns,
        columns=returns.columns,
    )

    cov_lw = CovarianceShrinkage(returns, returns_data=True).ledoit_wolf()
    cov_lw = pd.DataFrame(cov_lw, index=returns.columns, columns=returns.columns)

    cov = 0.5 * cov_garch + 0.5 * cov_lw
    return (cov + cov.T) / 2.0


def normalize_risk_budget(
    risk_budget: dict[str, float] | pd.Series | np.ndarray | None,
    asset_names: list[str],
) -> np.ndarray:
    """Return a non-negative normalized budget vector aligned to assets."""
    n = len(asset_names)
    if risk_budget is None:
        return np.ones(n) / n

    if isinstance(risk_budget, dict):
        rb = np.array([risk_budget.get(a, 0.0) for a in asset_names], dtype=float)
    elif isinstance(risk_budget, pd.Series):
        rb = risk_budget.reindex(asset_names).fillna(0.0).to_numpy(dtype=float)
    else:
        rb = np.asarray(risk_budget, dtype=float).reshape(-1)

    if rb.ndim != 1 or rb.size != n:
        raise ValueError("risk_budget must be a 1D vector matching asset_names.")
    if np.any(rb < 0) or rb.sum() <= 0:
        raise ValueError("risk_budget must be non-negative and sum to > 0.")

    return rb / rb.sum()


def optimize_risk_budget(
    cov_matrix: pd.DataFrame,
    asset_names: list[str],
    risk_budget: dict[str, float] | pd.Series | np.ndarray | None = None,
) -> pd.Series:
    """Solve risk-budgeting weights with Riskfolio-Lib."""
    configure_plotting()
    import riskfolio as rp

    n = len(asset_names)
    cov_aligned = cov_matrix.reindex(index=asset_names, columns=asset_names)

    port = rp.Portfolio(returns=pd.DataFrame(np.zeros((3, n)), columns=asset_names))
    port.cov = cov_aligned.values
    port.mu = pd.DataFrame(np.zeros((1, n)), columns=asset_names)

    rb = normalize_risk_budget(risk_budget, asset_names).reshape(-1, 1)
    weights = port.rp_optimization(model="Classic", rm="MV", b=rb, hist=False)

    if weights is None or "weights" not in weights.columns:
        raise ValueError("Risk budgeting optimization failed.")

    return pd.Series(weights["weights"].to_numpy(), index=asset_names, name="weights")


def walk_forward_backtest(
    prices: pd.DataFrame,
    cfg: PipelineConfig = CFG,
) -> tuple[pd.Series, pd.DataFrame]:
    """Run walk-forward rebalancing backtest with dividend reinvestment."""
    if prices.empty:
        raise ValueError("Input prices are empty.")

    returns = clean_returns(prices.pct_change().dropna())
    all_dates = [pd.Timestamp(d) for d in returns.index]

    port_rets: list[pd.Series] = []
    weights_hist: list[pd.Series] = []
    rebalance_dates: list = []
    previous_weights: pd.Series | None = None
    safe_total = sum(cfg.safe_target.values())
    risk_total = 1.0 - safe_total

    rebalance_steps = list(range(cfg.lookback_window, len(all_dates) - 1, cfg.rebalance_freq))
    for i in tqdm(rebalance_steps, desc="Backtest progress"):
        window_end = all_dates[i]

        window = returns.iloc[i - cfg.lookback_window : i + 1][cfg.risk_assets]
        window = clean_returns(window)
        if len(window) < int(cfg.lookback_window * 0.8):
            continue

        vols = forecast_garch_volatility(window, horizon=cfg.garch_horizon)
        cov = build_forecast_covariance(window, vols)

        rw = optimize_risk_budget(cov, asset_names=cfg.risk_assets, risk_budget=cfg.risk_budget)
        rw = rw.reindex(cfg.risk_assets).fillna(0.0)

        if rw.sum() <= 0:
            continue

        rw = rw / rw.sum()
        full_w = pd.Series(0.0, index=returns.columns, dtype=float)
        full_w.loc[cfg.risk_assets] = risk_total * rw
        full_w.loc[cfg.safe_assets] = pd.Series(cfg.safe_target, dtype=float)

        if previous_weights is None:
            turnover = float(full_w.sum())
        else:
            aligned_previous = previous_weights.reindex(full_w.index).fillna(0.0)
            turnover = float(np.abs(full_w - aligned_previous).sum() / 2.0)

        transaction_cost = turnover * cfg.transaction_cost_rate

        hold_end = min(i + cfg.rebalance_freq, len(all_dates) - 1)
        hold = clean_returns(returns.iloc[i + 1 : hold_end + 1])
        if hold.empty:
            continue

        full_w = full_w.reindex(hold.columns).fillna(0.0)
        
        for j, date in enumerate(hold.index):
            daily_ret = hold.loc[date].dot(full_w)
            if j == 0 and transaction_cost > 0:
                daily_ret = (1 + daily_ret) * (1 - transaction_cost) - 1
            port_rets.append(pd.Series(daily_ret, index=[date]))
        
        weights_hist.append(full_w)
        rebalance_dates.append(window_end)
        previous_weights = full_w.copy()

    if not port_rets:
        raise ValueError(
            "Backtest produced no out-of-sample returns. "
            "Try shorter lookback or check data coverage."
        )

    portfolio_returns = pd.concat(port_rets)
    weight_history = pd.DataFrame(weights_hist, index=rebalance_dates)
    return portfolio_returns, weight_history


def validate_config(cfg: PipelineConfig) -> None:
    """Validate required config constraints."""
    safe_total = sum(cfg.safe_target.values())
    if not 0.0 <= safe_total <= 1.0:
        raise ValueError("safe_target must sum to between 0 and 1.")
    if any(v < 0 for v in cfg.safe_target.values()):
        raise ValueError("All safe_target values must be non-negative.")
    if set(cfg.safe_target) & set(cfg.risk_budget):
        raise ValueError("Safe and risk buckets must not overlap.")
    if not 0.0 <= cfg.transaction_cost_rate <= 1.0:
        raise ValueError("transaction_cost_rate must be between 0 and 1.")
