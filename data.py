import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
from sklearn.datasets import load_wine


def format_int_ru(n: int) -> str:
    return f"{n:,}".replace(",", " ")


def corr_strength_ru(r: float) -> str:
    a = abs(r)
    if a < 0.10:
        strength = "ОЧЕНЬ СЛАБАЯ"
    elif a < 0.30:
        strength = "СЛАБАЯ"
    elif a < 0.50:
        strength = "УМЕРЕННАЯ"
    elif a < 0.70:
        strength = "ЗАМЕТНАЯ"
    elif a < 0.90:
        strength = "СИЛЬНАЯ"
    else:
        strength = "ОЧЕНЬ СИЛЬНАЯ"

    if r == 0:
        return "НЕТ ЛИНЕЙНОЙ СВЯЗИ"
    direction = "ПОЛОЖИТЕЛЬНАЯ" if r > 0 else "ОТРИЦАТЕЛЬНАЯ"
    return f"{strength} {direction} СВЯЗЬ"


def permutation_test_corr(x: pd.Series, y: pd.Series, n_permutations: int = 10000, seed: int = 42):
    # Односторонний тест: H1: corr(x,y) > 0
    x = x.dropna()
    y = y.loc[x.index].dropna()
    x = x.loc[y.index]

    r_obs = x.corr(y)

    rng = np.random.default_rng(seed)
    x_np = x.to_numpy()
    y_np = y.to_numpy()

    random_corrs = np.empty(n_permutations, dtype=float)
    for i in range(n_permutations):
        y_shuffled = rng.permutation(y_np)
        random_corrs[i] = pd.Series(x_np).corr(pd.Series(y_shuffled))

    p_value = float(np.mean(random_corrs >= r_obs))
    return r_obs, p_value, random_corrs


def print_report(x_name: str, y_name: str, r: float, p_value: float, n_perm: int, alpha: float = 0.05):
    min_p = 1 / n_perm

    print("ПРОВЕРКА ГИПОТЕЗЫ О ПОЛОЖИТЕЛЬНОЙ КОРРЕЛЯЦИИ\n")

    print("ГИПОТЕЗА:")
    print(f"Между {x_name} и {y_name} существует")
    print("статистически значимая положительная корреляция.")
    print(f"(Чем больше {x_name}, тем больше {y_name})\n")

    print("ФАКТИЧЕСКАЯ КОРРЕЛЯЦИЯ:")
    print(f"Коэффициент корреляции Пирсона: r = {r:.4f}")
    print(f"Качественная оценка: {corr_strength_ru(r)} (|r| = {abs(r):.3f})\n")

    print("СТАТИСТИЧЕСКАЯ ПРОВЕРКА (перестановочный тест):")
    print(f"Количество перестановок: {format_int_ru(n_perm)}")
    print(f"Вероятность случайного результата: p = {p_value:.10f}")
    print(f"(минимально достижимое значение: {min_p:.4f})\n")

    print("ОЦЕНОЧНЫЙ ВЫВОД:")
    if p_value < alpha and r > 0:
        print("✓ Гипотеза ПОДТВЕРЖДЕНА (высокостатистически значимая)")
    elif p_value < alpha and r <= 0:
        print("✓ Связь статистически значима, но НЕ положительная (проверь знак r)")
    else:
        print("✗ Гипотеза НЕ подтверждена (недостаточно оснований)")

    print(f"✓ Полученная корреляция (r = {r:.3f}) — {corr_strength_ru(r).lower()}")
    print(
        f"✓ Вероятность того, что такая связь возникла случайно: {p_value:.6f} "
        f"({'<' if p_value < alpha else '≥'} {alpha})"
    )


def main():
    # 1) Загрузка данных
    wine = load_wine(as_frame=True)
    df = wine.frame.copy()
    df = df.rename(columns={"target": "class_id"})
    df["class_name"] = df["class_id"].map({i: name for i, name in enumerate(wine.target_names)})

    # 2) CSV
    df.to_csv("wine_data.csv", index=False)

    # 3) Визуализация
    counts = df["class_name"].value_counts().reindex(wine.target_names)
    plt.figure(figsize=(10, 5))
    plt.bar(counts.index.astype(str), counts.values, edgecolor="black", alpha=0.9)
    plt.title("Распределение образцов по классам вина")
    plt.xlabel("Класс вина")
    plt.ylabel("Количество образцов")
    plt.grid(True, axis="y", alpha=0.25, linestyle=":")
    plt.tight_layout()
    plt.show()

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(10, 8))

    ax1.hist(df["alcohol"], bins=18, edgecolor="black", alpha=0.9)
    ax1.axvline(df["alcohol"].mean(), linestyle="--", linewidth=2,
                label=f"Среднее: {df['alcohol'].mean():.2f}")
    ax1.set_title("Гистограмма: Alcohol")
    ax1.grid(True, alpha=0.25, linestyle=":")
    ax1.legend()

    ax2.hist(df["color_intensity"], bins=18, edgecolor="black", alpha=0.9)
    ax2.axvline(df["color_intensity"].mean(), linestyle="--", linewidth=2,
                label=f"Среднее: {df['color_intensity'].mean():.2f}")
    ax2.set_title("Гистограмма: Color intensity")
    ax2.grid(True, alpha=0.25, linestyle=":")
    ax2.legend()

    plt.tight_layout()
    plt.show()

    # 4) Гипотеза + проверка
    x_name = "alcohol"
    y_name = "color_intensity"
    n_perm = 10000

    r_obs, p_value, random_corrs = permutation_test_corr(df[x_name], df[y_name], n_permutations=n_perm, seed=42)
    print_report(x_name, y_name, r_obs, p_value, n_perm=n_perm, alpha=0.05)

    # (доп) гистограмма распределения r при H0
    plt.figure(figsize=(10, 5))
    plt.hist(random_corrs, bins=30, edgecolor="black", alpha=0.9)
    plt.axvline(r_obs, linestyle="--", linewidth=2, label=f"Набл. r = {r_obs:.3f}")
    plt.title("Permutation Test: распределение r при H0 (перемешивание Y)")
    plt.xlabel("Коэффициент корреляции r")
    plt.ylabel("Частота")
    plt.grid(True, alpha=0.25, linestyle=":")
    plt.legend()
    plt.tight_layout()
    plt.show()


if __name__ == "__main__":
    main()
