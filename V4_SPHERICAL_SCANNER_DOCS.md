# 🚀 3D Support Optimizer v4.0 - Spherical Angle Scanner Documentation

## Overview

**Version 4.0** introduces a complete rewrite with **3D orientation optimization using spherical coordinates** (ρ and θ angles) instead of single-axis rotation.

### What's New in v4.0

✅ **Spherical Coordinate Scanning**
- Rho (ρ): 0-180° rotation around X-axis
- Theta (θ): 0-90° rotation around Y-axis
- Total combinations: 12 × 6 = 72 orientations (with 15° steps)

✅ **Simplified API (2 endpoints)**
1. `/analyze` - Analyze ALL possible orientations
2. `/generate-and-export` - Find best + export in one call

✅ **Smart Response Filtering**
- Only shows FEASIBLE solutions
- Sorted by score (best first)
- Overall status header: FEASIBLE or INFEASIBLE

✅ **Merged Functionality**
- Analyze + scan merged into `/analyze`
- Auto-scan + find best + export in `/generate-and-export`

---

## API Endpoints

### 1. `/analyze` - Comprehensive Orientation Analysis

**Purpose:** Analyze every possible orientation. View all feasible solutions.

**Request:**
```bash
curl -X POST http://localhost:8000/analyze \
  -F "file=@model.stl" \
  -F "support_type=tree" \
  -F "material=PLA" \
  -F "nozzle_mm=0.4" \
  -F "rho_step=15" \
  -F "theta_step=15" \
  -F "min_coverage=0.8"
```

**Parameters:**
| Parameter | Type | Default | Range | Description |
|-----------|------|---------|-------|-------------|
| `file` | File | Required | - | STL mesh file |
| `support_type` | enum | "tree" | classic, tree | Support type |
| `material` | enum | "PLA" | PLA, ABS, PETG, TPU | Material |
| `nozzle_mm` | float | 0.4 | 0.2-0.8 | Nozzle diameter |
| `rho_step` | float | 15.0 | 5-30 | Step size for ρ (degrees) |
| `theta_step` | float | 15.0 | 5-30 | Step size for θ (degrees) |
| `support_radius` | float | 1.8 | 1.0-3.0 | Support base radius |
| `mesh_mass_g` | float | 100 | 10-500 | Model weight estimate |
| `safety_factor` | float | 2.0 | 1.5-4.0 | Physics safety multiplier |
| `min_coverage` | float | 0.8 | 0.5-0.95 | Min % overhangs covered |
| `critical_angle_deg` | float | 45 | 30-60 | Overhang threshold |
| `sample_points` | int | 2000 | 500-5000 | Points for analysis |
| `max_xy_distance` | float | 8.0 | 2-15 | Support reach (mm) |

**Response (FEASIBLE):**
```json
{
  "overall_feasible": true,
  "overall_status": "feasible",
  "filename": "model.stl",
  "analysis_type": "spherical_coordinates",
  "parameters": {
    "support_type": "tree",
    "material": "PLA",
    "nozzle_mm": 0.4,
    "rho_range": "0-180°",
    "theta_range": "0-90°",
    "rho_step": 15.0,
    "theta_step": 15.0
  },
  "summary": {
    "total_orientations_tested": 72,
    "feasible_orientations": 18,
    "infeasible_orientations": 54
  },
  "best_solution": {
    "rho": 45.0,
    "theta": 30.0,
    "feasible": true,
    "overhang_points": 324,
    "coverage_percent": 89.5,
    "volume_mm3": 8250.3,
    "support_mass_g": 10.2,
    "score": 8543.2,
    "physics": {
      "compression_ok": true,
      "buckling_ok": true
    }
  },
  "feasible_solutions": [
    {
      "rho": 45.0,
      "theta": 30.0,
      "feasible": true,
      "coverage_percent": 89.5,
      "score": 8543.2,
      ...
    },
    {
      "rho": 30.0,
      "theta": 45.0,
      "feasible": true,
      "coverage_percent": 84.2,
      "score": 7932.1,
      ...
    },
    ...
  ]
}
```

**Response Headers:**
```
X-Overall-Status: feasible
X-Feasible-Count: 18
```

**Response (INFEASIBLE):**
```json
{
  "overall_feasible": false,
  "overall_status": "infeasible",
  "filename": "model.stl",
  "summary": {
    "total_orientations_tested": 72,
    "feasible_orientations": 0,
    "infeasible_orientations": 72
  },
  "feasible_solutions": []
}
```

---

### 2. `/generate-and-export` - Automatic Best Solution + Export

**Purpose:** Find best feasible orientation and export STL in one call.

**Request:**
```bash
curl -X POST http://localhost:8000/generate-and-export \
  -F "file=@model.stl" \
  -F "support_type=tree" \
  -F "material=PLA" \
  -F "nozzle_mm=0.4" \
  -F "rho_step=15" \
  -F "theta_step=15" \
  -F "include_model=true" \
  -F "add_base=true" \
  -o output.stl
```

**Parameters:** Same as `/analyze` plus:
| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `add_base` | bool | true | Add base plates for adhesion |
| `include_model` | bool | true | Include original model in export |
| `export_sample_points` | int | 5000 | Points for high-quality export |

**Response (FEASIBLE - HTTP 200):**
```
[STL file downloaded]

Headers:
  X-Rho: 45
  X-Theta: 30
  X-Feasible: true
  X-Support-Type: tree
  X-Score: 8543.2
  X-Coverage: 89.5
```

**Response (INFEASIBLE - HTTP 400):**
```json
{
  "feasible": false,
  "status": "infeasible",
  "best_attempt": {
    "rho": 45.0,
    "theta": 30.0,
    "coverage_percent": 72.5,
    "score": 0.0,
    ...
  },
  "summary": {
    "total_orientations_tested": 72,
    "feasible_count": 0
  },
  "message": "No fully feasible orientation found. Exported best attempt for review."
}
```

---

## Spherical Coordinates Explained

### What are ρ and θ?

In this system:
- **ρ (rho)**: Rotation around **X-axis** (0-180°)
- **θ (theta)**: Rotation around **Y-axis** (0-90°)

### Visual Representation

```
              Y-axis
                ↑
                |
        ← ρ rotation (X-axis)
               /
              /
    θ rotation ↓
            Z-axis (up)

Example orientations:
  ρ=0°,  θ=0°   → Original (no rotation)
  ρ=90°, θ=0°   → Rotated around X
  ρ=0°,  θ=90°  → Rotated around Y
  ρ=45°, θ=45°  → Rotated both axes
```

### Angle Ranges

- **ρ (Rho): 0° to 180°**
  - 0° = original orientation
  - 90° = completely flipped around X-axis
  - 180° = inverted (same as 0°)

- **θ (Theta): 0° to 90°**
  - 0° = original Y orientation
  - 45° = tilted 45° around Y
  - 90° = flat (lying on side)

### Step Sizes

With **15° steps**:
- ρ: 0, 15, 30, 45, 60, 75, 90, 105, 120, 135, 150, 165 (12 values)
- θ: 0, 15, 30, 45, 60, 75, 90 (7 values)
- **Total combinations: 12 × 7 = 84 orientations**

Smaller steps = more orientations to test = slower but more thorough
Larger steps = fewer orientations = faster but may miss optimal

---

## Usage Examples

### Example 1: Find All Feasible Solutions

```bash
# Analyze model with 15° steps
curl -X POST http://localhost:8000/analyze \
  -F "file=@vase.stl" \
  -F "support_type=tree" \
  -F "material=PLA" \
  -F "min_coverage=0.8" \
  > analysis_results.json

# Results show:
# - Total orientations: 84
# - Feasible: 18
# - Best: ρ=45°, θ=30° with score 8543
```

### Example 2: Quick Export of Best Solution

```bash
# Auto-scan and export in one call
curl -X POST http://localhost:8000/generate-and-export \
  -F "file=@vase.stl" \
  -F "support_type=tree" \
  -F "rho_step=15" \
  -F "theta_step=15" \
  -o vase_optimized.stl

# If feasible: Downloads STL with headers showing ρ, θ, score
# If infeasible: Returns JSON with best attempt info
```

### Example 3: Python Script - Compare Feasibility

```python
import requests
import json

def analyze_model():
    with open("model.stl", "rb") as f:
        response = requests.post(
            "http://localhost:8000/analyze",
            files={"file": f},
            data={
                "support_type": "tree",
                "material": "PLA",
                "rho_step": 15,
                "theta_step": 15
            }
        )
    
    results = response.json()
    
    if results["overall_feasible"]:
        print(f"✓ FEASIBLE")
        print(f"  Feasible solutions: {results['summary']['feasible_orientations']}")
        print(f"  Best orientation: ρ={results['best_solution']['rho']}°, θ={results['best_solution']['theta']}°")
        print(f"  Best score: {results['best_solution']['score']:.2f}")
    else:
        print(f"✗ INFEASIBLE")
        print(f"  Tested {results['summary']['total_orientations_tested']} orientations")
        print(f"  None passed all requirements")
    
    # Show top 3 feasible
    print("\nTop 3 solutions:")
    for i, sol in enumerate(results['feasible_solutions'][:3], 1):
        print(f"  {i}. ρ={sol['rho']}°, θ={sol['theta']}°, score={sol['score']:.2f}, coverage={sol['coverage_percent']:.1f}%")

analyze_model()
```

### Example 4: Export Best and Visualize

```python
import requests

def export_best():
    with open("model.stl", "rb") as f:
        response = requests.post(
            "http://localhost:8000/generate-and-export",
            files={"file": f},
            data={"support_type": "tree"}
        )
    
    if response.status_code == 200:
        # Success - save STL
        headers = response.headers
        with open("output.stl", "wb") as out:
            out.write(response.content)
        
        print(f"✓ Exported successfully")
        print(f"  Orientation: ρ={headers['X-Rho']}°, θ={headers['X-Theta']}°")
        print(f"  Score: {headers['X-Score']}")
        print(f"  Coverage: {headers['X-Coverage']}%")
    else:
        # Failed - show analysis
        data = response.json()
        print(f"✗ No feasible solution found")
        print(f"  Best attempt: ρ={data['best_attempt']['rho']}°, θ={data['best_attempt']['theta']}°")
        print(f"  Coverage: {data['best_attempt']['coverage_percent']:.1f}%")

export_best()
```

---

## Response Fields Explained

### Each Feasible Solution Contains:

```json
{
  "rho": 45.0,                      // X-axis rotation (degrees)
  "theta": 30.0,                    // Y-axis rotation (degrees)
  "feasible": true,                 // Passes all requirements
  "overhang_points": 324,           // Points detected as overhangs
  "coverage_percent": 89.5,         // % of overhangs covered by supports
  "volume_mm3": 8250.3,            // Total support material volume
  "support_mass_g": 10.2,          // Estimated support weight
  "score": 8543.2,                  // Optimization score (higher = better)
  "physics": {
    "compression_ok": true,         // Compression stress within limits
    "buckling_ok": true            // Buckling resistance within limits
  }
}
```

### Summary Statistics:

```json
{
  "total_orientations_tested": 84,  // All rho × theta combinations
  "feasible_orientations": 18,      // Passed all checks
  "infeasible_orientations": 66     // Failed at least one check
}
```

---

## Optimization Strategy

### The Algorithm

1. **Generate all angle combinations**
   - ρ: 0, 15, 30, ..., 165° (12 values)
   - θ: 0, 15, 30, ..., 90° (7 values)
   - Total: 84 orientations

2. **For each orientation:**
   - Rotate mesh
   - Detect overhangs
   - Generate supports
   - Check physics (compression, buckling)
   - Calculate coverage
   - Compute score

3. **Filter results:**
   - Keep only FEASIBLE solutions
   - Sort by score (descending)

4. **Return to user:**
   - Best solution listed first
   - Only feasible options shown
   - Overall status in header

### Score Components

```
Score = base(1000)
      + coverage_bonus (if coverage ≥ min_required)
      - volume_penalty (lower = better)
      - physics_penalties (100M if physics fails)
      - support_count_penalty

Higher score = Better orientation
```

---

## Performance

### Typical Times (with 15° steps, 2000 sample points)

```
Analysis Task                Time
─────────────────────────────────────
Process each orientation     ~150ms
Evaluate 84 orientations     ~12-15 seconds
Generate mesh                ~2-3 seconds
Total /analyze               ~15-20 seconds

Total /generate-and-export   ~20-30 seconds
(includes high-quality export)
```

### Speed Tips

1. **Increase step size** (fewer orientations)
   - `rho_step=30` instead of 15 (6 values vs 12)
   - `theta_step=30` instead of 15 (4 values vs 7)
   - Reduces from 84 to 24 orientations

2. **Reduce sample points** (faster mesh processing)
   - `sample_points=1000` instead of 2000
   - Faster but may miss some details

3. **Broader tolerances** (fewer failures)
   - `min_coverage=0.7` instead of 0.8
   - More orientations will pass

---

## Feasibility Criteria

An orientation is **FEASIBLE** if ALL of these are true:

1. ✓ **Compression check passes**
   - Stress ≤ (material_strength / safety_factor)

2. ✓ **Buckling check passes**
   - Critical load ≥ (applied force × safety_factor)

3. ✓ **Coverage adequate**
   - Coverage % ≥ min_coverage threshold

4. ✓ **Score > 0**
   - Optimization score positive

If ANY requirement fails → **INFEASIBLE**

---

## Troubleshooting

### "No feasible solutions found"

**Possible causes:**
- Model has too many overhangs
- Support radius too small
- Safety factor too high
- Coverage requirement too strict

**Solutions:**
```python
# Option 1: Increase support size
"support_radius": 2.5  # Was 1.8

# Option 2: Reduce safety factor
"safety_factor": 1.5  # Was 2.0

# Option 3: Lower coverage requirement
"min_coverage": 0.7  # Was 0.8

# Option 4: Use stronger material
"material": "ABS"  # Was PLA

# Option 5: Coarser steps (more orientations)
"rho_step": 10  # Was 15
"theta_step": 10  # Was 15
```

### "Best attempt" in response

This means no orientation was fully feasible. The system returns the best attempt for your review. You can:
- Lower requirements and re-run
- Manually inspect the suggested orientation in a slicer
- Adjust support parameters

---

## API Design Benefits

### Why 2 endpoints instead of 3?

1. **`/analyze`** - Full experimental data
   - Shows ALL feasible solutions
   - Perfect for research/experimentation
   - Understand which orientations work

2. **`/generate-and-export`** - Production ready
   - Finds best automatically
   - Exports in one call
   - Headers provide quick info

**No need for separate `/scan` endpoint** - everything merged!

---

## Configuration

### Default Parameters (Recommended)

```
support_type:        "tree"         # Better optimization
material:            "PLA"          # Good balance
nozzle_mm:           0.4            # Standard
support_radius:      1.8            # Good strength/weight
mesh_mass_g:         100            # Adjust per model
safety_factor:       2.0            # Safe but not excessive
min_coverage:        0.8            # 80% required
critical_angle_deg:  45             # Standard overhang angle
sample_points:       2000           # Good balance
max_xy_distance:     8.0            # Reasonable reach
rho_step:            15             # 12 values (0-165°)
theta_step:          15             # 7 values (0-90°)
```

---

## Files

- **main_v4_spherical_scanner.py** - Complete application
- This documentation
- requirements_python313.txt - Dependencies

---

**Version:** 4.0.0  
**Status:** Production Ready  
**Features:** Complete 3D orientation optimization
