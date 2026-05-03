# config.py - Application Configuration Template

from dataclasses import dataclass
from typing import Optional

@dataclass
class APIConfig:
    """API Configuration."""
    title: str = "3D Support Optimizer API"
    description: str = "Optimizes support structures for 3D printing"
    version: str = "2.0.0"
    host: str = "0.0.0.0"
    port: int = 8000
    debug: bool = False

@dataclass
class ProcessingConfig:
    """Processing Configuration."""
    max_sample_points: int = 3000
    default_critical_angle: float = 45.0
    default_tree_eps: float = 8.0
    default_branch_merge_eps: float = 2.5
    default_support_radius: float = 1.8

@dataclass
class PhysicsConfig:
    """Physics Calculation Configuration."""
    default_safety_factor: float = 2.0
    min_radius_multiplier: float = 2.5  # Multiplied by nozzle size
    gravity: float = 9.81  # m/s²

@dataclass
class MaterialConfig:
    """Material Configuration."""
    default_material: str = "PLA"
    allowed_materials: list = None
    
    def __post_init__(self):
        if self.allowed_materials is None:
            self.allowed_materials = ["PLA", "ABS", "PETG", "TPU"]

@dataclass
class NozzleConfig:
    """Nozzle Configuration."""
    allowed_sizes: list = None
    default_size: float = 0.4
    
    def __post_init__(self):
        if self.allowed_sizes is None:
            self.allowed_sizes = [0.2, 0.4, 0.6, 0.8]

@dataclass
class AppConfig:
    """Main Application Configuration."""
    api: APIConfig = None
    processing: ProcessingConfig = None
    physics: PhysicsConfig = None
    material: MaterialConfig = None
    nozzle: NozzleConfig = None
    
    def __post_init__(self):
        if self.api is None:
            self.api = APIConfig()
        if self.processing is None:
            self.processing = ProcessingConfig()
        if self.physics is None:
            self.physics = PhysicsConfig()
        if self.material is None:
            self.material = MaterialConfig()
        if self.nozzle is None:
            self.nozzle = NozzleConfig()

# Default configuration instance
config = AppConfig()

# Example: Load from environment
def load_config_from_env() -> AppConfig:
    """Load configuration from environment variables."""
    import os
    
    api_config = APIConfig(
        host=os.getenv("API_HOST", "0.0.0.0"),
        port=int(os.getenv("API_PORT", 8000)),
        debug=os.getenv("DEBUG", "False").lower() == "true"
    )
    
    processing_config = ProcessingConfig(
        max_sample_points=int(os.getenv("MAX_SAMPLE_POINTS", 3000)),
        default_critical_angle=float(os.getenv("CRITICAL_ANGLE", 45.0))
    )
    
    physics_config = PhysicsConfig(
        default_safety_factor=float(os.getenv("SAFETY_FACTOR", 2.0))
    )
    
    return AppConfig(
        api=api_config,
        processing=processing_config,
        physics=physics_config
    )
