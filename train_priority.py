"""
Input:  file Excel outbound tickets (cột: merchant, hour, dow, reachable, ...)
Output: models/priority_rf.pkl, models/priority_dt.pkl, models/priority_info.json
run: python -I train_priority.py --data path/to/tickets.xlsx
"""
import argparse
import json
import pickle
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.ensemble import RandomForestClassifier
from sklearn.tree import DecisionTreeClassifier
from sklearn.model_selection import cross_val_score, train_test_split

# Merchant category mapping 

MERCHANT_GROUPS = {
    "apple": ["APPLE.COM/BILL", "APPLE.COM"],
    "google": ["GOOGLE", "GOOGLE *"],
    "ai_subscription": [
        "SUNO INC.", "OPENROUTER", "CHATGPT", "CLAUDE", "MIDJOURNEY",
    ],
    "travel": ["AGODA", "TRIP.COM", "BOOKING", "AIRBNB", "TRAVELOKA"],
    "ecommerce": ["SHOPEE", "LAZADA", "TIKI", "SENDO"],
    "tiktok": ["TIKTOK", "9PAY*TIKTOK"],
    "gaming": ["STEAM", "RIOT", "GARENA", "MIHOYO", "ROBLOX"],
    "payment": ["VNPAY", "MOMO", "ZALOPAY", "PAYPAL", "ALIPAY", "STRIPE"],
    "telecom": ["VIETTELTELE", "SOFTBANKM"],
}

MERCHANT_CATEGORIES = [
    "ai_subscription", "apple", "ecommerce", "gaming", "google",
    "payment", "telecom", "tiktok", "travel", "other",
]


def classify_merchant(name: str) -> str:
    if not isinstance(name, str):
        return "other"
    upper = name.upper().strip()
    for cat, keywords in MERCHANT_GROUPS.items():
        for kw in keywords:
            if kw.upper() in upper:
                return cat
    return "other"


#  Feature engineering 

def hour_to_period(hour: int) -> int:
    if hour >= 22 or hour < 6:
        return 0  # đêm
    if hour < 12:
        return 1  # sáng
    if hour < 18:
        return 2  # chiều
    return 3      # tối


def build_features(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.Series]:
    df = df.copy()

    df["merchant_category"] = df["merchant"].apply(classify_merchant)
    df["mcat_idx"] = df["merchant_category"].apply(
        lambda c: MERCHANT_CATEGORIES.index(c)
        if c in MERCHANT_CATEGORIES
        else MERCHANT_CATEGORIES.index("other")
    )

    df["time_period"] = df["hour_of_day"].apply(hour_to_period)

    feature_cols = [
        "mcat_idx",          # merchant_category
        "hour_of_day",
        "day_of_week",
        "time_period",
        "customer_prev_reachable_rate",
        "customer_ticket_count",
        "is_nnldl",
        "has_note",
    ]

    X = df[feature_cols].astype(np.float32)
    y = df["is_reachable"].astype(int)
    return X, y


# Training 

def train(data_path: str, output_dir: str = "models"):
    out = Path(output_dir)
    out.mkdir(exist_ok=True)

    print(f"Loading data from {data_path} ...")
    df = pd.read_excel(data_path)
    print(f"  {len(df)} rows")

    X, y = build_features(df)
    print(f"  Features: {X.shape[1]}, target balance: "
          f"{y.mean():.1%} reach / {1 - y.mean():.1%} unreach")

    X_train, X_test, y_train, y_test = train_test_split(
        X, y, test_size=0.2, random_state=42, stratify=y,
    )
    print(f"  Train: {len(X_train)}, Test: {len(X_test)}")

    # Random Forest
    rf = RandomForestClassifier(
        n_estimators=100,
        max_depth=8,
        min_samples_leaf=30,
        class_weight="balanced",
        random_state=42,
        n_jobs=-1,
    )
    rf.fit(X_train, y_train)
    rf_cv = cross_val_score(rf, X_train, y_train, cv=5, scoring="f1").mean()
    print(f"  RF 5-fold CV F1: {rf_cv:.4f}")

    # Decision Tree
    dt = DecisionTreeClassifier(
        max_depth=6,
        min_samples_leaf=50,
        class_weight="balanced",
        random_state=42,
    )
    dt.fit(X_train, y_train)
    dt_cv = cross_val_score(dt, X_train, y_train, cv=5, scoring="f1").mean()
    print(f"  DT 5-fold CV F1: {dt_cv:.4f}")

    # Feature importance
    feat_names = [
        "merchant_category", "hour_of_day", "day_of_week", "time_period",
        "customer_prev_reachable_rate", "customer_ticket_count",
        "is_nnldl", "has_note",
    ]
    imp = rf.feature_importances_
    print("\n  Feature importance:")
    for name, val in sorted(zip(feat_names, imp), key=lambda x: -x[1]):
        print(f"    {name:35s} {val:.1%}")

    # Save
    with open(out / "priority_rf.pkl", "wb") as f:
        pickle.dump(rf, f)
    with open(out / "priority_dt.pkl", "wb") as f:
        pickle.dump(dt, f)

    info = {
        "features": feat_names,
        "merchant_categories": MERCHANT_CATEGORIES,
        "merchant_groups": MERCHANT_GROUPS,
        "target": "is_reachable (1=lien he duoc, 0=khong)",
        "train_size": len(X_train),
        "test_size": len(X_test),
        "dt_cv_f1": dt_cv,
        "rf_cv_f1": rf_cv,
    }
    with open(out / "priority_info.json", "w", encoding="utf-8") as f:
        json.dump(info, f, indent=2, ensure_ascii=False)

    print(f"\nSaved to {out}/")
    print("  priority_rf.pkl")
    print("  priority_dt.pkl")
    print("  priority_info.json")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Train priority scoring models")
    parser.add_argument("--data", required=True, help="Path to Excel file with ticket data")
    parser.add_argument("--output", default="models", help="Output directory (default: models)")
    args = parser.parse_args()
    train(args.data, args.output)
