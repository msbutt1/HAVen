"""HAVen local backend (Abduljabbar-owned).

Device adapters (simulated, serial, replay) feed one acquisition thread per
device; the signals package validates and processes every record; sessions,
calibrations and station records persist in local SQLite; a WebSocket hub
delivers live data without ever blocking acquisition.
"""

__version__ = "0.1.0"
