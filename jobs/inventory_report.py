"""Inventory report helpers."""

import pandas as pd


def stock_value(df: pd.DataFrame) -> float:
    total = 0.0
    for _, row in df.iterrows():
        total += row["qty"] * row["unit_cost"]
    return total


def low_stock(df: pd.DataFrame, threshold=5) -> pd.DataFrame:
    return df[df["qty"] < threshold]
