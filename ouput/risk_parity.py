"""Format risk-parity results without fetching or changing market data."""

import json


def format_risk_parity(result: dict, *, output_format: str = "table") -> str:
    if output_format == "json":
        return json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False)
    if output_format != "table":
        raise ValueError(f"Unsupported risk parity output format: {output_format}")
    headers = ("Rank", "Symbol", "Weight", "Risk budget", "Risk contribution")
    rows = [
        (
            str(row["rank"]), row["symbol"], f'{row["weight"]:.2%}',
            f'{row["risk_budget"]:.2%}', f'{row["risk_contribution"]:.2%}',
        )
        for row in result["weights"]
    ]
    widths = [
        max(len(header), *(len(row[i]) for row in rows)) if rows else len(header)
        for i, header in enumerate(headers)
    ]

    def line(values: tuple[str, ...]) -> str:
        return "| " + " | ".join(
            value.ljust(width) if i == 1 else value.rjust(width)
            for i, (value, width) in enumerate(zip(values, widths))
        ) + " |"

    border = "+-" + "-+-".join("-" * width for width in widths) + "-+"
    return "\n".join([
        f'Risk parity as of {result["as_of"]} | {result["method"]}',
        f'Lookback: {result["lookback"]} sessions | '
        f'{result["start_date"]} to {result["end_date"]} | '
        f'Annualized volatility: {result["annualized_volatility"]:.2%}',
        border, line(headers), border, *(line(row) for row in rows), border,
    ])
