import requests
import pandas as pd

API_URL = "http://127.0.0.1:8000/analyze"

models = {
    "Mando": "Mando.stl",
    "Bunny": "Stanford_Bunny.stl"
}

params = {
    "support_type": "tree",
    "material": "PLA",
    "nozzle_mm": 0.4,
    "support_radius": 1.8,

    "search_mode": "adaptive",
    "adaptive_initial_step": 60,
    "adaptive_min_step": 1,
    "adaptive_top_k": 2,

    "min_coverage": 0.3,
    "sample_points": 2000,

    "support_strategy": "pro",
    "pro_grid_size": 8,
    "pro_max_supports": 90,

    "orientation_mode": "upright",
    "max_upright_rho": 35,
    "max_upright_theta": 25,

    "reachability_filter": True,
    "fast_mode": True,
    "strict_collision": False
}

all_rows = []

for model_name, file_path in models.items():
    print(f"Analyzing {model_name}...")

    with open(file_path, "rb") as f:
        files = {
            "file": (file_path, f, "application/octet-stream")
        }

        response = requests.post(API_URL, params=params, files=files)

    if response.status_code != 200:
        print(f"Error for {model_name}:")
        print(response.text)
        continue

    data = response.json()

    # İstersen top_attempts yerine feasible_solutions da kullanabilirsin
    attempts = data.get("top_attempts", [])

    for i, item in enumerate(attempts):
        constraints = item.get("constraints", {})
        physics = item.get("physics", {})

        all_rows.append({
            "model": model_name,
            "index": i + 1,
            "rho": item.get("rho"),
            "theta": item.get("theta"),
            "score": item.get("score"),
            "coverage_percent": item.get("coverage_percent"),
            "volume_mm3": item.get("volume_mm3"),
            "support_mass_g": item.get("support_mass_g"),
            "support_count": item.get("support_count"),
            "feasible": item.get("feasible"),
            "collision_count": constraints.get("collision_count"),
            "disconnected_count": constraints.get("disconnected_count"),
            "compression_ok": physics.get("compression_ok"),
            "buckling_ok": physics.get("buckling_ok"),
            "reason": constraints.get("reason")
        })

df = pd.DataFrame(all_rows)
df.to_csv("orientation_results_two_models.csv", index=False)

print("Saved: orientation_results_two_models.csv")
print(df.head())