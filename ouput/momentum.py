"""Format and persist momentum results without fetching or changing market data."""

import json
from pathlib import Path
from tempfile import NamedTemporaryFile


DEFAULT_MOMENTUM_PATH = Path(__file__).resolve().parent / "momentum.json"

SCORE_DESCRIPTIONS = {
    "volatility_scaled": "RA score = cumulative return / sample daily volatility (not annualized)",
    "roc": "ROC score = cumulative simple return",
    "sharpe": "Sharpe score = mean daily return / sample daily volatility (zero risk-free rate; not annualized)",
    "regression": "Trend score = daily log-price regression slope * R-squared (not annualized)",
    "ensemble": "Score = negative three-group average rank; lower Avg rank is better",
}


def save_momentum(result: dict, path: Path) -> None:
    """Replace the saved result only after the complete JSON has been written."""
    content = format_momentum(result, output_format="json")
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, suffix=".tmp", delete=False,
    ) as handle:
        temporary = Path(handle.name)
        try:
            handle.write(content + "\n")
            handle.close()
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


def format_momentum(result: dict, *, output_format: str = "table") -> str:
    if output_format == "json":
        return json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False)
    if output_format != "table":
        raise ValueError(f"Unsupported momentum output format: {output_format}")

    method = result.get("scoring", {}).get("method", "volatility_scaled")
    headers = (
        "Rank", "Symbol", "Sources", "RA score" if method == "volatility_scaled" else "Score",
        "Return", "Daily vol", "Start date", "End date",
        "Start price", "End price",
    )
    rows = [
        (
            str(candidate["rank"]),
            candidate["symbol"],
            ", ".join(candidate["sources"]),
            f'{candidate["score"]:.4f}',
            f'{candidate["raw_return"]:.2%}',
            f'{candidate["daily_volatility"]:.2%}',
            candidate["start_date"],
            candidate["end_date"],
            f'{candidate["start_price"]:.4f}',
            f'{candidate["end_price"]:.4f}',
        )
        for candidate in result["candidates"]
    ]
    if method == "ensemble":
        headers += ("Avg rank", "Risk rank", "Vol rank", "Sharpe rank", "ROC rank", "Trend rank")
        rows = [
            row + (
                f'{candidate["average_rank"]:.4f}',
                f'{candidate["risk_adjusted_rank"]:.2f}',
                *(f'{candidate["method_ranks"][name]:.2f}' for name in (
                    "volatility_scaled", "sharpe", "roc", "regression",
                )),
            )
            for row, candidate in zip(rows, result["candidates"])
        ]
    widths = [
        max(len(header), *(len(row[index]) for row in rows))
        if rows else len(header)
        for index, header in enumerate(headers)
    ]
    numeric_columns = {0, 3, 4, 5, 8, 9}
    numeric_columns.update(range(10, len(headers)))

    def line(values: tuple[str, ...]) -> str:
        return "| " + " | ".join(
            value.rjust(width) if index in numeric_columns else value.ljust(width)
            for index, (value, width) in enumerate(zip(values, widths))
        ) + " |"

    border = "+-" + "-+-".join("-" * width for width in widths) + "-+"
    config = result["config"]
    output = [
        f'Momentum as of {result["as_of"]} | '
        f'Universe: {result["universe_size"]} | Eligible: {result["eligible_count"]} | '
        f'Shown: {len(rows)} | Excluded: {len(result["excluded"])}',
        f'Lookback: {config["lookback"]} sessions | Skip: {config["skip"]} sessions | '
        "Prices: forward-adjusted USD",
        f"Method: {method} | {SCORE_DESCRIPTIONS[method]}",
        border, line(headers), border,
        *(line(row) for row in rows),
        border,
    ]
    if not rows:
        output.append("No candidates.")
    return "\n".join(output)
