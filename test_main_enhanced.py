"""
Unit tests for 3D Support Optimizer
Tests core functionality without external dependencies
"""

import pytest
import numpy as np
from typing import Dict, Any

# Assuming the enhanced main module is imported
# from main_enhanced import (
#     GeometryProcessor, StructuralAnalyzer, SupportGenerator,
#     validate_nozzle, InvalidNozzleError, MATERIALS
# )


class TestGeometryProcessor:
    """Test suite for geometry processing functions."""
    
    def test_cylinder_area_calculation(self):
        """Test cylinder cross-sectional area calculation."""
        from main_enhanced import StructuralAnalyzer
        
        # Area = π * r²
        # For r=1, area should be π
        area = StructuralAnalyzer.cylinder_area_mm2(1.0)
        assert abs(area - np.pi) < 0.0001
        
        # For r=2, area should be 4π
        area = StructuralAnalyzer.cylinder_area_mm2(2.0)
        assert abs(area - 4 * np.pi) < 0.0001
    
    def test_cylinder_inertia_calculation(self):
        """Test second moment of inertia calculation."""
        from main_enhanced import StructuralAnalyzer
        
        # I = π * r⁴ / 4
        # For r=1, I should be π/4
        inertia = StructuralAnalyzer.cylinder_inertia_mm4(1.0)
        expected = np.pi / 4.0
        assert abs(inertia - expected) < 0.0001
    
    def test_point_load_estimation(self):
        """Test point load calculation."""
        from main_enhanced import StructuralAnalyzer
        
        # 100g mass, 1000 points
        load = StructuralAnalyzer.estimate_point_load_n(mesh_mass_g=100.0, point_count=1000)
        
        # Expected: (100/1000 * 9.81) = 0.981 N
        expected = (100.0 / 1000.0) * 9.81
        assert abs(load - expected) < 0.001
    
    def test_point_load_zero_points(self):
        """Test point load with zero points."""
        from main_enhanced import StructuralAnalyzer
        
        load = StructuralAnalyzer.estimate_point_load_n(mesh_mass_g=100.0, point_count=0)
        assert load == 0.0
    
    def test_point_load_half_mass(self):
        """Test point load scales with mass."""
        from main_enhanced import StructuralAnalyzer
        
        load1 = StructuralAnalyzer.estimate_point_load_n(mesh_mass_g=100.0, point_count=1000)
        load2 = StructuralAnalyzer.estimate_point_load_n(mesh_mass_g=50.0, point_count=1000)
        
        # Load should be half when mass is half
        assert abs(load2 - load1/2.0) < 0.001


class TestValidation:
    """Test suite for validation functions."""
    
    def test_valid_nozzle_sizes(self):
        """Test validation of allowed nozzle sizes."""
        from main_enhanced import validate_nozzle
        
        for nozzle in [0.2, 0.4, 0.6, 0.8]:
            result = validate_nozzle(nozzle)
            assert result == nozzle
    
    def test_invalid_nozzle_size(self):
        """Test validation rejects invalid nozzle sizes."""
        from main_enhanced import validate_nozzle, InvalidNozzleError
        
        with pytest.raises(InvalidNozzleError):
            validate_nozzle(0.5)  # Not in allowed list
    
    def test_string_nozzle_conversion(self):
        """Test nozzle validation converts strings."""
        from main_enhanced import validate_nozzle
        
        result = validate_nozzle("0.4")
        assert result == 0.4
        assert isinstance(result, float)


class TestStructuralAnalyzer:
    """Test suite for structural analysis."""
    
    def test_compression_check_passes(self):
        """Test compression check with safe parameters."""
        from main_enhanced import StructuralAnalyzer
        
        # Small force, large radius - should pass
        result = StructuralAnalyzer.compression_check(
            force_n=10.0,
            radius_mm=5.0,
            material="PLA",
            safety_factor=2.0
        )
        
        assert "stress_mpa" in result
        assert "allowable_mpa" in result
        assert "ok" in result
        assert isinstance(result["ok"], bool)
        assert result["stress_mpa"] < result["allowable_mpa"]
    
    def test_compression_check_fails(self):
        """Test compression check with unsafe parameters."""
        from main_enhanced import StructuralAnalyzer
        
        # Large force, tiny radius - should fail
        result = StructuralAnalyzer.compression_check(
            force_n=1000.0,
            radius_mm=0.1,
            material="TPU",  # Weakest material
            safety_factor=2.0
        )
        
        assert result["stress_mpa"] > result["allowable_mpa"]
        assert result["ok"] is False
    
    def test_buckling_check_zero_length(self):
        """Test buckling check with zero length."""
        from main_enhanced import StructuralAnalyzer
        
        result = StructuralAnalyzer.buckling_check(
            force_n=50.0,
            radius_mm=1.0,
            length_mm=0.0,
            material="PLA",
            safety_factor=2.0
        )
        
        assert result["critical_load_n"] == 0.0
        assert result["ok"] is False
    
    def test_required_radius_minimum(self):
        """Test required radius respects minimum size."""
        from main_enhanced import StructuralAnalyzer
        
        # Zero force with tiny nozzle should return minimum
        radius = StructuralAnalyzer.required_radius_for_force(
            force_n=0.0,
            length_mm=100.0,
            material="PLA",
            nozzle_mm=0.4,
            safety_factor=2.0
        )
        
        min_radius = 0.4 * 2.5  # nozzle_mm * 2.5
        assert radius >= min_radius
    
    def test_required_radius_scales_with_force(self):
        """Test that required radius increases with force."""
        from main_enhanced import StructuralAnalyzer
        
        radius1 = StructuralAnalyzer.required_radius_for_force(
            force_n=10.0,
            length_mm=50.0,
            material="PLA",
            nozzle_mm=0.4,
            safety_factor=2.0
        )
        
        radius2 = StructuralAnalyzer.required_radius_for_force(
            force_n=100.0,
            length_mm=50.0,
            material="PLA",
            nozzle_mm=0.4,
            safety_factor=2.0
        )
        
        assert radius2 > radius1


class TestMaterialProperties:
    """Test suite for material property management."""
    
    def test_material_exists(self):
        """Test that standard materials are defined."""
        from main_enhanced import MATERIALS
        
        expected_materials = ["PLA", "ABS", "PETG", "TPU"]
        for material in expected_materials:
            assert material in MATERIALS
    
    def test_material_properties_complete(self):
        """Test that materials have all required properties."""
        from main_enhanced import MATERIALS
        
        required_props = ["density_g_cm3", "compressive_strength_mpa", "elastic_modulus_mpa"]
        
        for material_name, props in MATERIALS.items():
            for prop in required_props:
                assert hasattr(props, prop), f"{material_name} missing {prop}"
                assert getattr(props, prop) > 0, f"{material_name}.{prop} must be positive"
    
    def test_material_properties_reasonable(self):
        """Test that material properties are within reasonable ranges."""
        from main_enhanced import MATERIALS
        
        for material_name, props in MATERIALS.items():
            # Density should be between 0.5 and 2.0 g/cm³ for plastics
            assert 0.5 <= props.density_g_cm3 <= 2.0, f"{material_name} density out of range"
            
            # Compressive strength should be 10-100 MPa
            assert 10 <= props.compressive_strength_mpa <= 100, \
                f"{material_name} compressive strength out of range"
            
            # Elastic modulus should be 50-4000 MPa
            assert 50 <= props.elastic_modulus_mpa <= 4000, \
                f"{material_name} elastic modulus out of range"


class TestSupportGenerator:
    """Test suite for support generation."""
    
    def test_classic_supports_empty_input(self):
        """Test classic support generation with empty points."""
        from main_enhanced import SupportGenerator
        
        points = np.empty((0, 3))
        supports = SupportGenerator.classic_supports(points)
        assert len(supports) == 0
    
    def test_classic_supports_single_point(self):
        """Test classic support generation with single point."""
        from main_enhanced import SupportGenerator
        
        points = np.array([[10.0, 20.0, 50.0]])
        supports = SupportGenerator.classic_supports(
            points,
            radius=1.5,
            min_height=1.0,
            bed_z=0.0
        )
        
        assert len(supports) == 1
        assert supports[0]["x"] == 10.0
        assert supports[0]["y"] == 20.0
        assert supports[0]["z_top"] == 50.0
        assert supports[0]["radius"] == 1.5
        assert supports[0]["height"] == 50.0
    
    def test_classic_supports_filters_short(self):
        """Test that very short supports are filtered."""
        from main_enhanced import SupportGenerator
        
        points = np.array([
            [10.0, 20.0, 50.0],  # Height 50.0 - should include
            [10.0, 20.0, 0.5]    # Height 0.5 - should skip (< min_height)
        ])
        
        supports = SupportGenerator.classic_supports(
            points,
            min_height=1.0,
            bed_z=0.0
        )
        
        assert len(supports) == 1


# Example of how to run tests:
# pytest test_main_enhanced.py -v
# pytest test_main_enhanced.py -v --cov=main_enhanced
