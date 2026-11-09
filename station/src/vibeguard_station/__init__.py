"""VibeGuard station: vibration-perception staircase test engine (Fahad-owned).

A prototype. The stimulus is a *motor setting*, not a measured amplitude, and the
result is not a diagnosis. See ``contract/STATION_CONTRACT.md``.

    from vibeguard_station import StationConfig, StationEngine, ResultStore
"""

from .adapters import (AdapterError, AdapterEvent, ScriptedResponder, SimResponse,
                       SimulatedStationAdapter, SimulatedSubject, VirtualClock)
from .config import StationConfig
from .engine import StationEngine, StationStateError
from .simulation import drive, run_simulated
from .store import ResultStore

__version__ = "0.1.0"
__all__ = ["AdapterError", "AdapterEvent", "ResultStore", "ScriptedResponder", "SimResponse",
           "SimulatedStationAdapter", "SimulatedSubject", "StationConfig", "StationEngine",
           "StationStateError", "VirtualClock", "drive", "run_simulated"]
