import pandas as pd
from sklearn.datasets import load_wine


def main():
    # 1) Загрузка датасета (кулинария: вино)
    wine = load_wine(as_frame=True)
    df = wine.frame.copy()
    df = df.rename(columns={"target": "class_id"})
    df["class_name"] = df["class_id"].map({i: name for i, name in enumerate(wine.target_names)})

    print("=== ОБЩАЯ ИНФОРМАЦИЯ О ДАННЫХ ===")
    print("Размер:", df.shape)
    print("Колонки:", list(df.columns))

    print("\nПервые 5 строк:")
    print(df.head())

    print("\nПропуски по колонкам:")
    print(df.isna().sum())


if __name__ == "__main__":
    main()
