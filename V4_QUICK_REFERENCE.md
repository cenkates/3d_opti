# ⚡ V4.0 Quick Reference

## What's New?

✅ **3D Spherical Angle Scanning** (ρ and θ)  
✅ **Only 2 Endpoints** (analyze, generate-and-export)  
✅ **Smart Response Filtering** (feasible only)  
✅ **Best Solution First** (sorted by score)  

---

## The Two Endpoints

### 1️⃣ `/analyze` - Analyze All Orientations

```bash
curl -X POST http://localhost:8000/analyze \
  -F "file=@model.stl"
```

**Response:**
```json
{
  "overall_feasible": true,
  "feasible_orientations": 18,
  "best_solution": { ... },
  "feasible_solutions": [ ... ]
}
```

**Use for:** Research, see all options

---

### 2️⃣ `/generate-and-export` - Find Best + Export

```bash
curl -X POST http://localhost:8000/generate-and-export \
  -F "file=@model.stl" \
  -o output.stl
```

**Response:**
- ✅ Feasible → Downloads STL with headers
- ❌ Infeasible → Returns JSON with best attempt

**Use for:** Production, get result immediately

---

## Angle Ranges

| Parameter | Range | Default Step | Values |
|-----------|-------|--------------|--------|
| **ρ (Rho)** | 0-180° | 15° | 12 angles |
| **θ (Theta)** | 0-90° | 15° | 7 angles |
| **Total** | - | - | **84 orientations** |

**ρ** = Rotation around X-axis  
**θ** = Rotation around Y-axis

---

## Key Parameters

```
support_type:    "tree" or "classic"
material:        "PLA" | "ABS" | "PETG" | "TPU"
nozzle_mm:       0.2 | 0.4 | 0.6 | 0.8
support_radius:  1.8 (adjust 1.0-3.0)
min_coverage:    0.8 (0.5-0.95)
safety_factor:   2.0 (1.5-4.0)
rho_step:        15 (smaller = more angles)
theta_step:      15 (smaller = more angles)
```

---

## Response Format

```json
{
  "overall_feasible": true|false,
  "overall_status": "feasible"|"infeasible",
  "summary": {
    "total_orientations_tested": 84,
    "feasible_orientations": 18
  },
  "best_solution": {
    "rho": 45,
    "theta": 30,
    "score": 8543.2,
    "coverage_percent": 89.5,
    "feasible": true
  },
  "feasible_solutions": [ ... ]
}
```

---

## Example: Python

```python
import requests

# Analyze
with open("model.stl", "rb") as f:
    r = requests.post(
        "http://localhost:8000/analyze",
        files={"file": f}
    )
results = r.json()

if results["overall_feasible"]:
    print(f"✓ Found {results['summary']['feasible_orientations']} solutions")
    print(f"Best: ρ={results['best_solution']['rho']}°, θ={results['best_solution']['theta']}°")
else:
    print("✗ No feasible solutions")
```

---

## Feasibility Status

**FEASIBLE** if:
- ✓ Compression check passes
- ✓ Buckling check passes  
- ✓ Coverage ≥ min_coverage
- ✓ Score > 0

**INFEASIBLE** if any fails

---

## Installation

```bash
# Install (Python 3.13)
pip install -r requirements_python313.txt

# Run
python -m uvicorn main_v4_spherical_scanner:app --reload

# Test
curl http://localhost:8000/health
```

---

## Speed Optimization

**Faster scanning:**
- Increase steps: `rho_step=30` (was 15)
- Reduce points: `sample_points=1000` (was 2000)
- Broader tolerances: `min_coverage=0.7` (was 0.8)

**More thorough:**
- Smaller steps: `rho_step=10` (was 15)
- More points: `sample_points=3000` (was 2000)
- Stricter requirements: `min_coverage=0.9` (was 0.8)

---

## Common Issues

**Problem:** No feasible solutions  
**Fix:**
```
↓ support_radius to 2.5
↓ safety_factor to 1.5
↓ min_coverage to 0.7
↑ material strength (ABS)
```

**Problem:** Too many orientations (slow)  
**Fix:** Increase rho_step and theta_step

**Problem:** Missing optimal angle  
**Fix:** Decrease rho_step and theta_step

---

## File Location

**Application:** `/outputs/main_v4_spherical_scanner.py`  
**Dependencies:** `requirements_python313.txt`  
**Docs:** `V4_SPHERICAL_SCANNER_DOCS.md`

---

**Version:** 4.0.0 | **Status:** ✅ Ready to Use
