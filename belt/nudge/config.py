"""Typed configuration loaded from config.toml.

Every setting has a default here, so config.toml only needs the values that
differ. Unknown keys are an error, which catches typos before a walk.
"""

from __future__ import annotations

import dataclasses
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, get_type_hints


class ConfigError(ValueError):
    pass


@dataclass
class PodConfig:
    channel: int = 0
    rest_deg: float = 90.0
    contact_deg: float = 90.0


@dataclass
class ActuatorConfig:
    # "servo": the PCA9685, through ServoKit if installed, else the built-in driver.
    # "pca9685": always the built-in driver. "mock": log only.
    driver: str = "servo"
    i2c_bus: int = 1  # /dev/i2c-1: pins 3 and 5
    i2c_address: int = 0x40
    pwm_freq_hz: int = 50
    min_pulse_us: int = 500
    max_pulse_us: int = 2400
    angle_min_deg: float = 0.0
    angle_max_deg: float = 180.0
    hold_s: float = 0.12  # time the arm stays on the shoulder
    settle_s: float = 0.15  # time allowed for the arm to lift off again
    double_gap_s: float = 0.08  # extra pause between the two taps of a double tap
    stagger_s: float = 0.06  # offset between the two pods in stop()
    min_interval_s: float = 0.4  # rate limit between steering taps
    left: PodConfig = field(default_factory=lambda: PodConfig(0, 90.0, 50.0))
    right: PodConfig = field(default_factory=lambda: PodConfig(1, 90.0, 130.0))


@dataclass
class SensorConfig:
    source: str = "phone"  # "phone", "sim" or "imu"
    heading_source: str = "compass"  # "compass" or "gyro" (see README)
    stale_after_s: float = 1.0
    calibrate_window_s: float = 0.5
    # Average the heading over this long before using it (0 = off, 2 at most).
    # For a phone that swings with each stride, such as one in a pocket.
    heading_smooth_s: float = 0.0


@dataclass
class DeadReckonConfig:
    step_length_m: float = 0.7
    # The height of the wearer step_length_m was measured on. Setting another
    # wearer's height (POST /wearer, or the viewer's slider) scales the step
    # length in proportion.
    height_ref_cm: float = 175.0
    # Drift correction (Localizer). Setting a distance or time to 0 turns
    # that one rule off; drift_correction = false turns them all off.
    drift_correction: bool = True
    snap_dist_m: float = 1.5
    turn_window_m: float = 3.0
    # On a long leg the step count can be further out than that by the time
    # the corner comes. The window is this fraction of the leg's length when
    # that is more than turn_window_m (0 = always turn_window_m).
    turn_window_frac: float = 0.0
    turn_tol_deg: float = 25.0
    turn_time_s: float = 3.0
    # The turn must still be there this long after it is first seen, so a
    # single bad compass reading cannot snap the pose to a corner. 0 = off.
    turn_hold_s: float = 0.15
    min_turn_deg: float = 45.0
    # Off by default: on a short first leg one step more or less is a large
    # error (about 14 percent on 5 m). Measure the wearer's step instead.
    calibrate_step_length: bool = False
    calibrate_min_leg_m: float = 0.0  # a first leg shorter than this measures nothing
    step_length_min_m: float = 0.5
    step_length_max_m: float = 0.9
    bias_tau_s: float = 5.0
    # Headings further than this off the leg are not "walking the leg" and are
    # not learned as bias. Keep it at or below follower.enter_deg, so that a
    # heading the follower is tapping to correct is never learned away.
    bias_max_deg: float = 20.0
    freeze_after_s: float = 1.5
    freeze_heading: bool = False
    # Ignore steps reported this long after a tap is issued (0 = off). For
    # use if the servo arms shake the plate enough to register as steps.
    step_blank_after_tap_s: float = 0.0


@dataclass
class FollowerConfig:
    enter_deg: float = 20.0  # start correcting above this heading error
    exit_deg: float = 10.0  # go quiet again below this
    urgent_deg: float = 60.0  # double taps above this
    tap_period_s: float = 1.0
    arrive_radius_m: float = 1.0
    turning_deg_s: float = 25.0  # no new tap while the wearer turns the right way faster than this; 0 = off
    overturn_window_s: float = 4.0  # an error on the other side this soon after a tap is an overturn; 0 = off
    overturn_gap_s: float = 0.5  # an overturn is answered this soon after the last tap, not tap_period_s


@dataclass
class ServerConfig:
    host: str = "0.0.0.0"
    port: int = 8000
    tls_port: int = 8443
    cert_dir: str = "certs"


@dataclass
class RouteConfig:
    default: str = "routes/demo_L.json"
    dir: str = "routes"
    flip_y: bool = False
    # Drop route points within this distance of the line through their
    # neighbours (0 = follow every point). For routes from a planner.
    simplify_m: float = 0.0
    # A route that changes floor: true = wait at the change and carry on when
    # told the wearer is on the next floor; false = stop at the first change.
    floors: bool = True
    # A new route mid-walk is picked up where the wearer is if they are
    # within this distance of it; otherwise they are first led to its start.
    rejoin_m: float = 2.0


@dataclass
class ObstacleConfig:
    enabled: bool = False  # the switch as it stands at start-up; the viewer flips it while running
    stop_m: float = 1.5  # something this close dead ahead: stop the wearer
    clear_m: float = 2.0  # once stopped, the way counts as clear again beyond this
    side_room_m: float = 1.2  # a side is worth tapping towards if nothing there is nearer than this
    stale_s: float = 1.0  # a reading older than this is not used
    tap_period_s: float = 1.0  # while blocked: one tap towards the open side this often


@dataclass
class LoggingConfig:
    dir: str = "logs"


@dataclass
class Config:
    actuators: ActuatorConfig = field(default_factory=ActuatorConfig)
    sensors: SensorConfig = field(default_factory=SensorConfig)
    deadreckon: DeadReckonConfig = field(default_factory=DeadReckonConfig)
    follower: FollowerConfig = field(default_factory=FollowerConfig)
    server: ServerConfig = field(default_factory=ServerConfig)
    route: RouteConfig = field(default_factory=RouteConfig)
    obstacle: ObstacleConfig = field(default_factory=ObstacleConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Config":
        cfg = _merge(cls(), data, "")
        cfg.validate()
        return cfg

    def validate(self) -> None:
        a = self.actuators
        if a.driver not in ("servo", "pca9685", "mock"):
            raise ConfigError(f"actuators.driver must be 'servo', 'pca9685' or 'mock', not {a.driver!r}")
        if not 0 <= a.angle_min_deg < a.angle_max_deg <= 180:
            raise ConfigError("actuators angle_min_deg/angle_max_deg must satisfy 0 <= min < max <= 180")
        if a.left.channel == a.right.channel:
            raise ConfigError("actuators.left and actuators.right must use different channels")
        for pod in (a.left, a.right):
            if not 0 <= pod.channel <= 15:
                raise ConfigError("PCA9685 channels are 0-15")
        if a.stagger_s < 0.05:
            raise ConfigError("actuators.stagger_s must be at least 0.05 (servos never start together)")
        for name in ("hold_s", "settle_s", "double_gap_s", "min_interval_s"):
            if getattr(a, name) < 0:
                raise ConfigError(f"actuators.{name} must not be negative")
        if self.sensors.source not in ("phone", "sim", "imu"):
            raise ConfigError("sensors.source must be 'phone', 'sim' or 'imu'")
        if self.sensors.heading_source not in ("compass", "gyro"):
            raise ConfigError("sensors.heading_source must be 'compass' or 'gyro'")
        if self.sensors.stale_after_s <= 0:
            raise ConfigError("sensors.stale_after_s must be positive")
        if not 0 <= self.sensors.heading_smooth_s <= 2.0:
            raise ConfigError("sensors.heading_smooth_s must be between 0 and 2 seconds")
        d = self.deadreckon
        if d.step_length_m <= 0:
            raise ConfigError("deadreckon.step_length_m must be positive")
        if d.height_ref_cm <= 0:
            raise ConfigError("deadreckon.height_ref_cm must be positive")
        if not 0 < d.step_length_min_m <= d.step_length_max_m:
            raise ConfigError("deadreckon step_length_min_m/max_m must satisfy 0 < min <= max")
        if not 0 <= d.turn_window_frac <= 0.5 or d.calibrate_min_leg_m < 0:
            raise ConfigError("deadreckon.turn_window_frac must be between 0 and 0.5, calibrate_min_leg_m not negative")
        if min(d.bias_max_deg, d.bias_tau_s, d.freeze_after_s, d.snap_dist_m, d.turn_window_m,
               d.turn_tol_deg, d.turn_time_s, d.turn_hold_s, d.min_turn_deg) < 0:
            raise ConfigError("deadreckon distances, times and angles must not be negative")
        if self.route.simplify_m < 0 or self.route.rejoin_m < 0:
            raise ConfigError("route.simplify_m and rejoin_m must not be negative")
        o = self.obstacle
        if not 0 < o.stop_m <= o.clear_m:
            raise ConfigError("obstacle stop_m/clear_m must satisfy 0 < stop_m <= clear_m")
        if o.side_room_m < 0 or o.stale_s <= 0 or o.tap_period_s <= 0:
            raise ConfigError("obstacle.side_room_m must not be negative; stale_s and tap_period_s must be positive")
        f = self.follower
        if not 0 <= f.exit_deg < f.enter_deg <= f.urgent_deg <= 180:
            raise ConfigError("follower angles must satisfy 0 <= exit_deg < enter_deg <= urgent_deg <= 180")
        if f.tap_period_s <= 0 or f.arrive_radius_m <= 0:
            raise ConfigError("follower.tap_period_s and arrive_radius_m must be positive")
        if min(f.turning_deg_s, f.overturn_window_s, f.overturn_gap_s) < 0:
            raise ConfigError("follower.turning_deg_s, overturn_window_s and overturn_gap_s must not be negative")


def _merge(base: Any, data: dict[str, Any], where: str) -> Any:
    """Return a copy of dataclass `base` with `data` applied on top."""
    if not isinstance(data, dict):
        raise ConfigError(f"[{where}] must be a table")
    hints = get_type_hints(type(base))
    changes: dict[str, Any] = {}
    for key, value in data.items():
        path = f"{where}.{key}" if where else key
        if key not in hints:
            raise ConfigError(f"unknown setting: {path}")
        typ = hints[key]
        if dataclasses.is_dataclass(typ):
            changes[key] = _merge(getattr(base, key), value, path)
        elif typ is bool:
            if not isinstance(value, bool):
                raise ConfigError(f"{path} must be true or false")
            changes[key] = value
        elif typ is float:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ConfigError(f"{path} must be a number")
            changes[key] = float(value)
        elif typ is int:
            if isinstance(value, bool) or not isinstance(value, int):
                raise ConfigError(f"{path} must be a whole number")
            changes[key] = value
        else:
            if not isinstance(value, typ):
                raise ConfigError(f"{path} must be {typ.__name__}")
            changes[key] = value
    return dataclasses.replace(base, **changes)


def find_config(explicit: str | None = None) -> Path | None:
    """--config wins, then ./config.toml, then the one beside the package."""
    if explicit:
        path = Path(explicit)
        if not path.is_file():
            raise ConfigError(f"config file not found: {path}")
        return path
    for candidate in (Path.cwd() / "config.toml", Path(__file__).resolve().parent.parent / "config.toml"):
        if candidate.is_file():
            return candidate
    return None


def load_config(explicit: str | None = None) -> tuple[Config, Path]:
    """Load the config and return it with the directory other paths are relative to."""
    path = find_config(explicit)
    if path is None:
        return Config.from_dict({}), Path.cwd()
    with path.open("rb") as fh:
        data = tomllib.load(fh)
    return Config.from_dict(data), path.resolve().parent
