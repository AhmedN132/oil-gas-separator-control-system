"""Basic automated consistency tests: python tests/test_simulator.py from repository root."""
import math
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from separator_sim import Config, liquid_volume, level_from_volume, simulate

c=Config()
for h in [0.0,0.1,0.4,1.0,1.6,1.9,2.0]:
    assert math.isclose(level_from_volume(liquid_volume(h,c),c),h,abs_tol=1e-8)
assert math.isclose(liquid_volume(1.0,c),math.pi*(c.diameter_m/2)**2*c.straight_length_m/2,rel_tol=1e-10)
base,bs=simulate("baseline",c)
assert abs(bs["final_level_m"]-1.0)<1e-6
assert abs(bs["final_pressure_kPag"]-1000)<1e-6
step,ss=simulate("liquid_step",c)
assert max(r["LCV_101_open_pct"] for r in step)>50
high,hs=simulate("high_level",c)
assert hs["first_LAHH_s"] is not None
low,ls=simulate("low_level",c)
assert ls["first_LALL_s"] is not None
trip,ts=simulate("pressure_trip",c)
assert ts["SIS_trip_time_s"] is not None
assert all(r["SDV_101_closed"]==1 and r["liquid_inlet_m3h"]==0 for r in trip if r["time_s"]>=ts["SIS_trip_time_s"])
print("PASS: geometry, baseline, controller direction, alarms, and latched SIS trip")
