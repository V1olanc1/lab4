import pandas as pd
import matplotlib.pyplot as plt
from sklearn.datasets import load_wine


def main():
    # 1) Загрузка датасета
    wine = load_wine(as_frame=True)
    df = wine.frame.copy()
    df = df.rename(columns={"target": "class_id"})
    df["class_name"] = df["class_id"].map({i: name for i, name in enumerate(wine.target_names)})

    # 2) Сохранение CSV
    df.to_csv("wine_data.csv", index=False)

    # 3.1 Диаграмма распределения по классам
    counts = df["class_name"].value_counts().reindex(wine.target_names)
    plt.figure(figsize=(10, 5))
    plt.bar(counts.index.astype(str), counts.values, edgecolor="black", alpha=0.9)
    plt.title("Распределение образцов по классам вина")
    plt.xlabel("Класс вина")
    plt.ylabel("Количество образцов")
    plt.grid(True, axis="y", alpha=0.25, linestyle=":")
    plt.tight_layout()
    plt.show()

    # 3.2 Гистограммы Alcohol и Color intensity
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(10, 8))

    ax1.hist(df["alcohol"], bins=18, edgecolor="black", alpha=0.9)
    ax1.axvline(df["alcohol"].mean(), linestyle="--", linewidth=2,
                label=f"Среднее: {df['alcohol'].mean():.2f}")
    ax1.set_title("Гистограмма: Alcohol")
    ax1.set_xlabel("Alcohol")
    ax1.set_ylabel("Частота")
    ax1.grid(True, alpha=0.25, linestyle=":")
    ax1.legend()

    ax2.hist(df["color_intensity"], bins=18, edgecolor="black", alpha=0.9)
    ax2.axvline(df["color_intensity"].mean(), linestyle="--", linewidth=2,
                label=f"Среднее: {df['color_intensity'].mean():.2f}")
    ax2.set_title("Гистограмма: Color intensity")
    ax2.set_xlabel("Color intensity")
    ax2.set_ylabel("Частота")
    ax2.grid(True, alpha=0.25, linestyle=":")
    ax2.legend()

    plt.tight_layout()
    plt.show()


if __name__ == "__main__":
    main()
