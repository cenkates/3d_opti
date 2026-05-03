# Quick Start Guide - 3D Support Optimizer v2.0

## Overview
The enhanced version of your 3D Support Optimizer includes significant improvements in code quality, error handling, documentation, and performance.

## What's New?

### ✨ Key Improvements
1. **Object-Oriented Architecture** - Code organized into logical classes
2. **Better Error Handling** - Custom exceptions and HTTP error codes
3. **Comprehensive Logging** - Track operations and debug issues
4. **Full Type Hints** - Better IDE support and type safety
5. **Complete Documentation** - Docstrings and examples throughout
6. **Unit Tests** - Foundation for testing your code
7. **Configuration Management** - Centralized settings
8. **Production Ready** - Better resource handling and cleanup

## Installation

### 1. Install Dependencies
```bash
pip install -r requirements.txt
```

### 2. Run the Server
```bash
# Development mode
python -m uvicorn main_enhanced:app --reload

# Production mode
uvicorn main_enhanced:app --host 0.0.0.0 --port 8000
```

### 3. Access the API
- **API Root:** http://localhost:8000/
- **Interactive Docs:** http://localhost:8000/docs
- **Alternative Docs:** http://localhost:8000/redoc

## File Structure

```
project/
├── main_enhanced.py          # Main application (refactored)
├── config.py                 # Configuration management
├── requirements.txt          # Python dependencies
├── test_main_enhanced.py     # Unit tests
├── ENHANCEMENTS.md          # Detailed enhancement report
└── README.md                 # This file
```

## API Endpoints

### Health Check
```bash
curl http://localhost:8000/
curl http://localhost:8000/health
```

### Analyze Supports
```bash
curl -X POST http://localhost:8000/analyze-supports \
  -F "file=@model.stl" \
  -F "material=PLA" \
  -F "nozzle_mm=0.4" \
  -F "angle=60"
```

## Usage Examples

### Python Client Example
```python
import requests

# Upload and analyze a model
with open("model.stl", "rb") as f:
    files = {"file": f}
    params = {
        "material": "PLA",
        "nozzle_mm": 0.4,
        "angle": 60.0,
        "mesh_mass_g": 100.0,
        "safety_factor": 2.0
    }
    
    response = requests.post(
        "http://localhost:8000/analyze-supports",
        files=files,
        params=params
    )
    
    result = response.json()
    print(f"Overhang points: {result['overhang_points']}")
    print(f"Support structures: {len(result['supports']['trunks'])} trunks")
```

### Using the Classes Directly
```python
from main_enhanced import (
    GeometryProcessor, StructuralAnalyzer, SupportGenerator
)
import trimesh
import numpy as np

# Load mesh
mesh = trimesh.load_mesh("model.stl")

# Detect overhangs at 45-degree angle
overhang_faces = GeometryProcessor.detect_overhang_faces(mesh, critical_angle_deg=45.0)

# Get centers of overhang faces
overhang_points = GeometryProcessor.face_centers(mesh, overhang_faces)

# Calculate required radius for a 50N force
radius = StructuralAnalyzer.required_radius_for_force(
    force_n=50.0,
    length_mm=100.0,
    material="PLA",
    nozzle_mm=0.4,
    safety_factor=2.0
)

# Check compression stress
compression = StructuralAnalyzer.compression_check(
    force_n=50.0,
    radius_mm=radius,
    material="PLA",
    safety_factor=2.0
)
print(f"Compression OK: {compression['ok']}")

# Check buckling
buckling = StructuralAnalyzer.buckling_check(
    force_n=50.0,
    radius_mm=radius,
    length_mm=100.0,
    material="PLA",
    safety_factor=2.0
)
print(f"Buckling OK: {buckling['ok']}")
```

## Configuration

### Method 1: Using config.py
```python
from config import config

print(config.api.title)                    # API title
print(config.processing.max_sample_points) # Max points
print(config.physics.default_safety_factor) # Safety factor
```

### Method 2: Environment Variables
```bash
export API_HOST="0.0.0.0"
export API_PORT="8000"
export CRITICAL_ANGLE="45"
export SAFETY_FACTOR="2.0"
python main_enhanced.py
```

### Method 3: Direct Configuration
```python
from config import AppConfig, APIConfig

config = AppConfig(
    api=APIConfig(
        host="0.0.0.0",
        port=8000,
        debug=True
    )
)
```

## Running Tests

### Install Testing Dependencies
```bash
pip install pytest pytest-cov
```

### Run All Tests
```bash
pytest test_main_enhanced.py -v
```

### Run Specific Test Class
```bash
pytest test_main_enhanced.py::TestGeometryProcessor -v
```

### Run with Coverage Report
```bash
pytest test_main_enhanced.py --cov=main_enhanced --cov-report=html
```

## Common Tasks

### Adding a New Material
```python
from main_enhanced import MATERIALS, MaterialProperties

# In MATERIALS dictionary
"Carbon Fiber": MaterialProperties(
    density_g_cm3=1.55,
    compressive_strength_mpa=80.0,
    elastic_modulus_mpa=5000.0
)
```

### Adding a New Nozzle Size
```python
from main_enhanced import ALLOWED_NOZZLES

ALLOWED_NOZZLES.append(1.0)
```

### Custom Physics Calculation
```python
from main_enhanced import StructuralAnalyzer

# Check multiple loading scenarios
for mass_g in [50, 100, 150, 200]:
    load = StructuralAnalyzer.estimate_point_load_n(mass_g, 1000)
    radius = StructuralAnalyzer.required_radius_for_force(
        force_n=load,
        length_mm=100,
        material="PLA",
        nozzle_mm=0.4
    )
    print(f"Mass {mass_g}g → Radius {radius:.2f}mm")
```

## Error Handling

### Try-Catch Pattern
```python
from main_enhanced import InvalidNozzleError, validate_nozzle

try:
    nozzle = validate_nozzle(0.5)
except InvalidNozzleError as e:
    print(f"Invalid nozzle: {e}")
```

### API Error Responses
```python
import requests

response = requests.post(
    "http://localhost:8000/analyze-supports",
    files=files,
    params={"nozzle_mm": 0.5}  # Invalid nozzle
)

if response.status_code == 400:
    error = response.json()
    print(f"Error: {error['detail']}")
```

## Performance Tips

### 1. Optimize Point Sampling
```python
# Reduce sample points for faster analysis
GeometryProcessor.downsample_points(points, max_points=1000)
```

### 2. Use Appropriate Materials
```python
# PLA is default and fast
# Choose based on actual use case
```

### 3. Batch Analysis
```python
# Process multiple angles at once
for angle in range(0, 360, 45):
    mesh, points, bed_z = GeometryProcessor.process_rotated_mesh(
        original_mesh, angle
    )
    # Process...
```

## Troubleshooting

### Issue: "ModuleNotFoundError: No module named 'trimesh'"
**Solution:** Run `pip install -r requirements.txt`

### Issue: "InvalidNozzleError: nozzle_mm must be one of..."
**Solution:** Use only supported nozzles: 0.2, 0.4, 0.6, 0.8

### Issue: "Port 8000 already in use"
**Solution:** Use a different port:
```bash
uvicorn main_enhanced:app --port 8001
```

### Issue: High memory usage
**Solution:** Reduce max_sample_points or process in batches

## Next Steps

1. **Add Database** - Store analysis results and history
2. **Add Authentication** - Secure API access
3. **Add Caching** - Speed up repeated analyses
4. **Add Async Tasks** - Handle long-running operations
5. **Add Webhooks** - Notify users when analysis completes
6. **Add CLI** - Command-line interface for power users
7. **Add WebUI** - Web-based interface for visualization
8. **Add Docker** - Easy deployment in containers

## Support & Documentation

- **Detailed Enhancements:** See `ENHANCEMENTS.md`
- **API Documentation:** http://localhost:8000/docs
- **Type Hints:** Full IDE autocomplete support
- **Docstrings:** Comprehensive inline documentation

## Version Info

- **Version:** 2.0.0
- **Python:** 3.8+
- **FastAPI:** 0.104.1
- **NumPy:** 1.24.3
- **Trimesh:** 4.0.1

## License & Attribution

Built on enhanced version of 3D Support Optimizer.
Improvements include OOP refactoring, error handling, and comprehensive documentation.

---

**Happy optimizing! 🚀**
