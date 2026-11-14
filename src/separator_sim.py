"""V-101 educational two-phase separator dynamic simulation.

Run from repository root: python src/separator_sim.py --scenario all
Requirements: numpy, matplotlib

NOT for plant operation, relief sizing, SIS design, or certified safety analysis.
Thermodynamic vessel model is simplified (ideal-gas, isothermal, equilibrium assumed).
"""
from __future__ import annotations

import argparse
import csv
import math
from dataclasses import dataclass
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

R = 8.314462618  # J/(mol K)
P_ATM_KPA = 101.325
STD_T_K = 288.15  # 15 deg C, explicitly assumed for Sm3


@dataclass
class Config:
    diameter_m: float = 2.0
    straight_length_m: float = 6.0  # cylindrical section ONLY; vessel heads excluded
    normal_level_m: float = 1.0
    operating_pressure_kpag: float = 1000.0
    operating_temperature_c: float = 35.0
    liquid_feed_m3h: float = 20.0
    gas_feed_sm3h: float = 1000.0
    downstream_pressure_kpag: float = 300.0  # ASSUMPTION, both outlets
    liquid_alarm_hh_m: float = 1.6  # ASSUMPTION (80% diameter)
    liquid_alarm_ll_m: float = 0.4  # ASSUMPTION (20% diameter)
    pressure_trip_hh_kpag: float = 1400.0  # ASSUMPTION, not approved setpoint
    liquid_valve_bias: float = 0.5
    gas_valve_bias: float = 0.5
    liquid_valve_time_constant_s: float = 12.0
    gas_valve_time_constant_s: float = 5.0
    level_kp_per_m: float = 0.9
    level_ki_per_m_s: float = 0.002
    pressure_kp_per_kpa: float = 0.002
    pressure_ki_per_kpa_s: float = 0.00004
    duration_s: int = 3600
    step_s: float = 1.0
    disturbance_time_s: float = 600.0


def clamp(x: float, lo: float, hi: float) -> float:
    return min(hi, max(lo, x))


def liquid_volume(h: float, c: Config) -> float:
    """Horizontal cylinder segment V(h); excludes end heads."""
    r = c.diameter_m / 2
    h = clamp(h, 0.0, c.diameter_m)
    z = clamp((r-h)/r, -1.0, 1.0)
    area = r*r*math.acos(z) - (r-h)*math.sqrt(max(0.0, 2*r*h-h*h))
    return c.straight_length_m * area


def level_from_volume(volume: float, c: Config) -> float:
    """Invert the segment-volume equation with bounded bisection."""
    maximum = math.pi*(c.diameter_m/2)**2*c.straight_length_m
    volume = clamp(volume, 0.0, maximum)
    lo, hi = 0.0, c.diameter_m
    for _ in range(38):
        mid = (lo+hi)/2
        if liquid_volume(mid, c) < volume:
            lo = mid
        else:
            hi = mid
    return (lo+hi)/2


class PI:
    """Direct-acting PI with simple conditional integration anti-windup."""
    def __init__(self, bias: float, kp: float, ki: float):
        self.bias, self.kp, self.ki = bias, kp, ki
        self.integral = 0.0

    def update(self, measured: float, setpoint: float, dt: float) -> float:
        # DIRECT acting: a higher level/pressure must open the respective outlet valve.
        error = measured - setpoint
        candidate_integral = self.integral + error*dt
        raw_candidate = self.bias + self.kp*error + self.ki*candidate_integral
        if (0 <= raw_candidate <= 1) or (raw_candidate > 1 and error < 0) or (raw_candidate < 0 and error > 0):
            self.integral = candidate_integral
        return clamp(self.bias + self.kp*error + self.ki*self.integral, 0, 1)


def simulate(name: str, c: Config) -> tuple[list[dict], dict]:
    valid = {"baseline", "liquid_step", "gas_step", "high_level", "low_level", "stuck_lcv", "pressure_trip"}
    if name not in valid:
        raise ValueError(f"Unknown scenario: {name}; choose from {sorted(valid)}")
    if c.step_s <= 0 or c.duration_s <= 0:
        raise ValueError("Positive duration and step required")
    if c.operating_pressure_kpag <= c.downstream_pressure_kpag:
        raise ValueError("Operating pressure must exceed downstream pressure for these valve models")
    max_volume = math.pi*(c.diameter_m/2)**2*c.straight_length_m
    vol = liquid_volume(c.normal_level_m, c)
    T = c.operating_temperature_c + 273.15
    p_abs_pa = (c.operating_pressure_kpag+P_ATM_KPA)*1000
    n_gas = p_abs_pa*(max_volume-vol)/(R*T)
    std_molar_feed_mols = c.gas_feed_sm3h * P_ATM_KPA*1000/(R*STD_T_K)/3600
    liq_nom_m3s = c.liquid_feed_m3h/3600
    liquid_pi = PI(c.liquid_valve_bias, c.level_kp_per_m, c.level_ki_per_m_s)
    gas_pi = PI(c.gas_valve_bias, c.pressure_kp_per_kpa, c.pressure_ki_per_kpa_s)
    lcv, pcv = c.liquid_valve_bias, c.gas_valve_bias
    sdv_closed = False
    trip_time_s = None
    rows: list[dict] = []
    was_hh = was_ll = False
    first_hh_s = first_ll_s = None
    delta_p_nom = c.operating_pressure_kpag-c.downstream_pressure_kpag

    for k in range(int(c.duration_s/c.step_s)+1):
        t = k*c.step_s
        h = level_from_volume(vol,c)
        gas_volume = max_volume-vol
        pressure_kpag = n_gas*R*T/gas_volume/1000-P_ATM_KPA
        after = t >= c.disturbance_time_s
        q_liq_in = liq_nom_m3s
        gas_in = std_molar_feed_mols
        if after and name == "liquid_step": q_liq_in *= 1.4
        if after and name == "gas_step": gas_in *= 1.3
        if after and name == "high_level": q_liq_in *= 3.0
        if after and name == "low_level": q_liq_in *= 0.15
        if after and name == "pressure_trip": gas_in *= 1.75

        # SIS uses PT-102 (same physical pressure in this deterministic model, independent channel concept).
        # Pressure setpoint is illustrative, and SDV closure is latched with no automatic reset.
        if pressure_kpag >= c.pressure_trip_hh_kpag and not sdv_closed:
            sdv_closed = True
            trip_time_s = t
        if sdv_closed:
            q_liq_in = 0.0
            gas_in = 0.0

        lcv_cmd = liquid_pi.update(h,c.normal_level_m,c.step_s)
        pcv_cmd = gas_pi.update(pressure_kpag,c.operating_pressure_kpag,c.step_s)
        lcv += (lcv_cmd-lcv)*min(1,c.step_s/c.liquid_valve_time_constant_s)
        pcv += (pcv_cmd-pcv)*min(1,c.step_s/c.gas_valve_time_constant_s)
        if after and name == "stuck_lcv": lcv = 0.2
        if after and name == "low_level": lcv = 0.9  # simultaneous outlet stuck-open failure
        if after and name == "pressure_trip": pcv = 0.1  # simulated stuck-restricted PCV

        # Outlet rates: simplified flow vs opening and differential pressure.
        dp_ratio = max(pressure_kpag-c.downstream_pressure_kpag,0)/delta_p_nom
        q_liq_out = liq_nom_m3s*(lcv/c.liquid_valve_bias)*math.sqrt(dp_ratio)
        gas_out = std_molar_feed_mols*(pcv/c.gas_valve_bias)*math.sqrt(dp_ratio)
        hh = h >= c.liquid_alarm_hh_m
        ll = h <= c.liquid_alarm_ll_m
        if hh and not was_hh and first_hh_s is None: first_hh_s=t
        if ll and not was_ll and first_ll_s is None: first_ll_s=t
        was_hh,was_ll=hh,ll

        rows.append({
            "time_s":round(t,3),"level_m":h,"level_setpoint_m":c.normal_level_m,
            "liquid_inlet_m3h":q_liq_in*3600,"liquid_outlet_m3h":q_liq_out*3600,
            "LCV_101_open_pct":lcv*100,"LCV_command_pct":lcv_cmd*100,
            "pressure_kPag":pressure_kpag,"pressure_setpoint_kPag":c.operating_pressure_kpag,
            "gas_inlet_Sm3h":gas_in*R*STD_T_K/(P_ATM_KPA*1000)*3600,
            "gas_outlet_Sm3h":gas_out*R*STD_T_K/(P_ATM_KPA*1000)*3600,
            "PCV_101_open_pct":pcv*100,"PCV_command_pct":pcv_cmd*100,
            "LAHH_101":int(hh),"LALL_101":int(ll),"PAHH_101_trip":int(sdv_closed),
            "SDV_101_closed":int(sdv_closed),
        })
        if k == int(c.duration_s/c.step_s): break
        vol = clamp(vol+(q_liq_in-q_liq_out)*c.step_s,0,0.99*max_volume)  # numerical limit; overflow not modelled
        n_gas = max(0.0001, n_gas+(gas_in-gas_out)*c.step_s)

    summary = {
        "scenario":name,"initial_level_m":rows[0]["level_m"],"final_level_m":rows[-1]["level_m"],
        "min_level_m":min(r["level_m"] for r in rows),"max_level_m":max(r["level_m"] for r in rows),
        "initial_pressure_kPag":rows[0]["pressure_kPag"],
        "max_pressure_kPag":max(r["pressure_kPag"] for r in rows),
        "final_pressure_kPag":rows[-1]["pressure_kPag"],
        "first_LAHH_s":first_hh_s,"first_LALL_s":first_ll_s,
        "SIS_trip_time_s":trip_time_s,
    }
    return rows, summary


def save_results(rows: list[dict], summary: dict, outdir: Path, c: Config) -> None:
    outdir.mkdir(parents=True,exist_ok=True)
    name = summary["scenario"]
    with (outdir/f"{name}.csv").open("w",newline="",encoding="utf-8") as f:
        writer=csv.DictWriter(f,fieldnames=list(rows[0].keys()))
        writer.writeheader();writer.writerows(rows)

    t=np.array([r["time_s"]/60 for r in rows])
    fig,ax=plt.subplots(3,1,figsize=(11,9),sharex=True,layout="constrained")
    ax[0].plot(t,[r["level_m"] for r in rows],label="LT-101 level")
    ax[0].axhline(rows[0]["level_setpoint_m"],ls="--",label="Setpoint")
    ax[0].axhline(c.liquid_alarm_hh_m,ls=":",label="LAHH (assumed)")
    ax[0].axhline(c.liquid_alarm_ll_m,ls=":",label="LALL (assumed)")
    ax[0].set_ylabel("Liquid level (m)");ax[0].legend(loc="best");ax[0].grid(alpha=.2)
    ax[1].plot(t,[r["LCV_101_open_pct"] for r in rows],label="LCV-101 actual")
    ax[1].plot(t,[r["LCV_command_pct"] for r in rows],ls="--",label="LIC-101 command")
    ax[1].set_ylabel("LCV opening (%)");ax[1].set_ylim(-5,105);ax[1].legend();ax[1].grid(alpha=.2)
    ax[2].plot(t,[r["pressure_kPag"] for r in rows],label="PT-101 / PT-102 pressure")
    ax[2].axhline(rows[0]["pressure_setpoint_kPag"],ls="--",label="Pressure setpoint")
    ax[2].axhline(c.pressure_trip_hh_kpag,ls=":",label="SIS trip (assumed)")
    ax[2].set_ylabel("Pressure (kPag)");ax[2].set_xlabel("Time (minutes)")
    ax[2].legend();ax[2].grid(alpha=.2)
    fig.suptitle(f"V-101 | {name.replace('_',' ').title()} | Simplified dynamic model")
    fig.savefig(outdir/f"{name}.png",dpi=155)
    plt.close(fig)


def main() -> None:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario",default="all",choices=["all","baseline","liquid_step","gas_step","high_level","low_level","stuck_lcv","pressure_trip"])
    parser.add_argument("--duration",type=int,default=3600,help="Simulation duration in seconds")
    parser.add_argument("--output",default=str(Path(__file__).resolve().parents[1] / "results"),help="Output directory (default: repository results/)")
    args=parser.parse_args()
    c=Config(duration_s=args.duration)
    scenarios=["baseline","liquid_step","gas_step","high_level","low_level","stuck_lcv","pressure_trip"] if args.scenario=="all" else [args.scenario]
    outdir=Path(args.output)
    summaries=[]
    for name in scenarios:
        rows,s=simulate(name,c)
        save_results(rows,s,outdir,c)
        summaries.append(s)
        print(f"{name:14s} final level={s['final_level_m']:.3f} m | "
              f"max pressure={s['max_pressure_kPag']:.1f} kPag | "
              f"LAHH={s['first_LAHH_s']} s | LALL={s['first_LALL_s']} s | "
              f"SIS trip={s['SIS_trip_time_s']} s")
    with (outdir/"summary.csv").open("w",newline="",encoding="utf-8") as f:
        writer=csv.DictWriter(f,fieldnames=list(summaries[0].keys()))
        writer.writeheader();writer.writerows(summaries)
    print(f"\nResults saved in: {outdir.resolve()}")

if __name__ == "__main__":
    main()
