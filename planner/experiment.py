"""Thực nghiệm bộ lập lịch thích ứng thời tiết trên thời tiết lịch sử.

    python -m planner.experiment run [--config eval/planner_experiment.yml] [--time-limit 1] [--llm-dir DIR]
    python -m planner.experiment llm-collect [--model gpt-5.4-mini] [--reasoning-effort none]
    python -m planner.experiment llm-collect --base-url http://localhost:11434/v1 --model qwen3.5:9b --workers 1
    python -m planner.experiment llm-prompt --date 2025-10-27 [--trip-days 1]
    python -m planner.experiment llm-score eval/llm_schedules/2025-10-27.json

`run` ghi data/qa/planner_experiment_<ngày>.md và .csv. Cấu hình và ý nghĩa ba
biến thể aware / baseline / oracle: xem eval/planner_experiment.yml.
"""

import argparse
import csv
import json
import re
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path

import yaml

from pipeline.ingest.weather import grid_key
from planner import rules
from planner.evaluator import weather_outcome
from planner.feasibility import check_feasibility
from planner.llm_baseline import llm_prompt, schedule_from_json
from planner.matrix import TravelMatrix
from planner.models import ScheduleResult, UserConstraint
from planner.places import load_places_by_qid
from planner.solver import SolverOptions, WeatherAwareScheduleOptimizer
from planner.weather import (
    HOURLY, NONE, FetchJson, GridWeather, default_fetch_json, fetch_observed, fetch_previous_runs,
    tier_for_lead,
)
from rag.benchmark import paired_bootstrap

DEFAULT_CONFIG = "eval/planner_experiment.yml"
DEFAULT_OUT_DIR = "data/qa"
VARIANTS = ("aware", "baseline", "oracle")
RAINY_DAY_HOURS = 2  # ngày có từ chừng này giờ mưa trong khung đi chơi được tính là ngày mưa


@dataclass
class Scenario:
    name: str
    trip_days: int
    step_days: int


@dataclass
class ExperimentConfig:
    places: list[str]
    hotel: tuple[float, float]
    transport_mode: str
    day_start: time
    day_end: time
    max_places_per_day: int
    lead_days: int
    periods: list[tuple[date, date]]
    scenarios: list[Scenario]


def load_config(path: str = DEFAULT_CONFIG) -> ExperimentConfig:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    return ExperimentConfig(
        places=list(raw["places"]),
        hotel=tuple(raw["hotel"]),
        transport_mode=raw.get("transport_mode", "motorbike"),
        day_start=time.fromisoformat(raw.get("day_start", "08:00")),
        day_end=time.fromisoformat(raw.get("day_end", "18:00")),
        max_places_per_day=int(raw.get("max_places_per_day", 4)),
        lead_days=int(raw.get("lead_days", 1)),
        periods=[(date.fromisoformat(str(a)), date.fromisoformat(str(b))) for a, b in raw["periods"]],
        scenarios=[Scenario(**s) for s in raw["scenarios"]],
    )


def load_weather(fetch_json: FetchJson, locations, periods, lead_days: int) -> tuple[GridWeather, GridWeather]:
    """(dự báo phát hành trước `lead_days` ngày, quan trắc ERA5) cho mọi ô lưới cần dùng."""
    forecast, observed = GridWeather(), GridWeather()
    for grid in sorted({grid_key(lat, lon) for lat, lon in locations}):
        for start, end in periods:
            forecast.add_hourly(grid, fetch_previous_runs(fetch_json, grid, start, end, lead_days))
            observed.add_hourly(grid, fetch_observed(fetch_json, grid, start, end))
    return forecast, observed


def constraint_for(cfg: ExperimentConfig, start: date, trip_days: int) -> UserConstraint:
    return UserConstraint(
        start_datetime=datetime.combine(start, cfg.day_start),
        end_datetime=datetime.combine(start + timedelta(days=trip_days - 1), cfg.day_end),
        start_location=cfg.hotel, end_location=cfg.hotel, day_start=cfg.day_start, day_end=cfg.day_end,
        max_places_per_day=cfg.max_places_per_day, transport_mode=cfg.transport_mode,
    )


def _has_data(w: GridWeather, lat: float, lon: float, day: date) -> bool:
    return w.observed_at(lat, lon, datetime.combine(day, time(12))) is not None


def rain_hours(observed: GridWeather, cfg: ExperimentConfig, day: date) -> int:
    """Số giờ mưa quan trắc tại khách sạn trong khung đi chơi."""
    return sum(
        1 for h in range(cfg.day_start.hour, cfg.day_end.hour)
        if (w := observed.observed_at(*cfg.hotel, datetime.combine(day, time(h)))) and rules.is_raining(w)
    )


def evaluate_variant(result: ScheduleResult, places: dict, constraint: UserConstraint,
                     observed: GridWeather) -> dict:
    outcome = weather_outcome(result, places, observed)
    feas = check_feasibility(result, places, constraint)
    return {
        "visits": outcome.visits,
        "outdoor_rain_hours": outcome.outdoor_rain_hours,
        "hazard_visits": outcome.hazard_visits,
        "travel_minutes": sum(d.total_travel_minutes for d in result.itineraries),
        "feasible": int(feas.feasible),
        "violations": ";".join(sorted({v.kind for v in feas.violations})),
        "hazard_details": " | ".join(outcome.hazard_details),
    }


def iter_trips(cfg: ExperimentConfig, forecast: GridWeather, observed: GridWeather,
               max_trips: int | None = None):
    """(kịch bản, ngày đầu, các ngày) của mọi chuyến; đặt tầng thời tiết cho các ngày
    của chuyến trước khi trả ra, nên mọi biến thể thấy cùng dự báo."""
    tier = tier_for_lead(cfg.lead_days)
    for scenario in cfg.scenarios:
        trips = 0
        for period_start, period_end in cfg.periods:
            start = period_start
            while start + timedelta(days=scenario.trip_days - 1) <= period_end:
                if max_trips is not None and trips >= max_trips:
                    break
                days = [start + timedelta(days=i) for i in range(scenario.trip_days)]
                for day in days:
                    forecast.set_tier(day, tier if _has_data(forecast, *cfg.hotel, day) else NONE)
                    observed.set_tier(day, HOURLY)
                yield scenario, start, days
                trips += 1
                start += timedelta(days=scenario.step_days)


def llm_schedule_path(llm_dir: str | Path, trip_days: int, start: date) -> Path:
    return Path(llm_dir) / f"{trip_days}d" / f"{start.isoformat()}.json"


def load_llm_schedule(path: Path, by_id: dict) -> ScheduleResult | None:
    """Lịch LLM đã lưu; None khi câu trả lời không đọc được theo JSON_FORMAT."""
    if not path.exists():
        raise SystemExit(f"Thiếu {path}. Chạy `python -m planner.experiment llm-collect` trước,"
                         " cùng --max-trips với lần chấm.")
    try:
        return schedule_from_json(path.read_text(encoding="utf-8"), by_id)
    except (ValueError, KeyError, TypeError, AttributeError):
        return None


def fill_travel(result: ScheduleResult, matrix: TravelMatrix, node: dict) -> None:
    """Điền thời gian đi từ điểm trước cho lịch LLM (JSON không có trường này), cùng
    ma trận với bộ lập lịch để cột di chuyển so được. Nút 0 là khách sạn."""
    for itinerary in result.itineraries:
        prev = 0
        for v in sorted(itinerary.visits, key=lambda v: v.arrival_time):
            if v.place_id in node:
                v.travel_minutes_from_prev = matrix.minutes(prev, node[v.place_id])
                prev = node[v.place_id]


def run(conn, cfg: ExperimentConfig, time_limit_s: float = 1.0, fetch_json: FetchJson | None = None,
        out_dir: str | None = DEFAULT_OUT_DIR, max_trips: int | None = None,
        maps_client=None, llm_dir: str | None = None) -> tuple[str, list[dict]]:
    """`maps_client`: nguồn thời gian đi không phụ thuộc giờ (OSRM); None thì ước lượng.
    Google không dùng được ở đây vì Routes API không nhận giờ khởi hành trong quá khứ.
    `llm_dir`: thư mục lịch do `llm-collect` lưu; có thì thêm biến thể `llm`."""
    places = load_places_by_qid(conn, cfg.places)
    by_id = {p.id: p for p in places}
    fetch_json = fetch_json or default_fetch_json(conn)
    locations = [cfg.hotel] + [(p.lat, p.lon) for p in places]
    forecast, observed = load_weather(fetch_json, locations, cfg.periods, cfg.lead_days)

    aware = WeatherAwareScheduleOptimizer(SolverOptions(time_limit_s=time_limit_s), maps_client=maps_client)
    blind = WeatherAwareScheduleOptimizer(SolverOptions(weather_aware=False, time_limit_s=time_limit_s),
                                          maps_client=maps_client)
    if llm_dir:
        llm_matrix = TravelMatrix([cfg.hotel] + [(p.lat, p.lon) for p in places], cfg.transport_mode,
                                  maps_client=maps_client)
        llm_node = {p.id: i + 1 for i, p in enumerate(places)}

    rows: list[dict] = []
    for scenario, start, days in iter_trips(cfg, forecast, observed, max_trips):
        constraint = constraint_for(cfg, start, scenario.trip_days)
        plans = {
            "aware": aware.optimize(places, constraint, forecast),
            "baseline": blind.optimize(places, constraint, None),
            "oracle": aware.optimize(places, constraint, observed),
        }
        if llm_dir:
            plans["llm"] = load_llm_schedule(llm_schedule_path(llm_dir, scenario.trip_days, start), by_id)
            if plans["llm"] is not None:
                fill_travel(plans["llm"], llm_matrix, llm_node)
        observed_rain = sum(rain_hours(observed, cfg, d) for d in days)
        for variant, result in plans.items():
            if result is None:  # LLM trả lời sai định dạng: tính là lịch rỗng, không khả thi
                metrics = evaluate_variant(ScheduleResult(solver_status="llm_invalid"), by_id, constraint,
                                           observed)
                metrics.update(feasible=0, violations="invalid_output")
                status = "llm_invalid"
            else:
                metrics = evaluate_variant(result, by_id, constraint, observed)
                status = result.solver_status
            rows.append({
                "scenario": scenario.name, "start": start.isoformat(), "variant": variant,
                "forecast_tier": ",".join(forecast.tier(d) for d in days),
                "observed_rain_hours": observed_rain,
                "rainy": int(any(rain_hours(observed, cfg, d) >= RAINY_DAY_HOURS for d in days)),
                "solver_status": status,
                **metrics,
            })

    travel = "đường thật (OSRM)" if aware.last_matrix_source == "osrm" else "ước lượng từ khoảng cách"
    report = render_report(cfg, rows, time_limit_s, travel, llm_note=_llm_note(llm_dir))
    if out_dir:
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        stem = f"planner_experiment_{date.today().isoformat()}"
        if aware.last_matrix_source == "osrm":
            stem += "_osrm"
        if llm_dir:
            stem += f"_llm_{Path(llm_dir).name}"
        (out / f"{stem}.md").write_text(report, encoding="utf-8")
        with (out / f"{stem}.csv").open("w", encoding="utf-8", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
    return report, rows


def _mean(values) -> float:
    values = list(values)
    return sum(values) / len(values) if values else 0.0


def _llm_note(llm_dir: str | None) -> str | None:
    if not llm_dir:
        return None
    meta = Path(llm_dir) / "meta.json"
    info = json.loads(meta.read_text(encoding="utf-8")) if meta.exists() else {}
    provider = f" qua {info['provider']}" if info.get("provider") else ""
    return (f"- llm: LLM tự lập lịch từ cùng dữ liệu và cùng dự báo với aware (`{info.get('model', '?')}`{provider},"
            f" reasoning_effort `{info.get('reasoning_effort', '?')}`, prompt `planner/llm_baseline.py`)."
            " Câu trả lời sai định dạng tính là lịch rỗng, không khả thi.")


def render_report(cfg: ExperimentConfig, rows: list[dict], time_limit_s: float,
                  travel: str = "ước lượng từ khoảng cách", llm_note: str | None = None) -> str:
    present = [v for v in (*VARIANTS, "llm") if any(r["variant"] == v for r in rows)]
    lines = [
        f"# Thực nghiệm lập lịch thích ứng thời tiết — {date.today().isoformat()}",
        "",
        f"- Địa điểm: {len(cfg.places)} điểm, tối đa {cfg.max_places_per_day} điểm/ngày,"
        f" khung {cfg.day_start:%H:%M}-{cfg.day_end:%H:%M}, {cfg.transport_mode}.",
        f"- Giai đoạn: " + ", ".join(f"{a}..{b}" for a, b in cfg.periods) + ".",
        f"- Lập lịch trên dự báo phát hành trước {cfg.lead_days} ngày (Open-Meteo Previous Runs,"
        f" tầng `{tier_for_lead(cfg.lead_days)}`); chấm trên thời tiết quan trắc ERA5.",
        f"- Solver: OR-Tools, giới hạn {time_limit_s:g} giây mỗi lịch. Thời gian đi: {travel}.",
        "- aware: có thời tiết, dùng dự báo. baseline: tắt thời tiết. oracle: có thời tiết, biết trước"
        " thời tiết thật (cận trên, cho biết phần thua do dự báo sai).",
        *([llm_note] if llm_note else []),
        "",
        "Chỉ số: `rain_h` = giờ ở ngoài trời khi đang mưa (giờ × tỷ lệ ngoài trời); `hazard` = số lượt"
        " đến điểm đang có hiện tượng nguy hiểm trong `unsafe_conditions` của nó; `visits` = số điểm"
        " đi được; `feasible` = tỷ lệ lịch không vi phạm giờ mở cửa, thời gian đi, khung ngày.",
    ]
    by_key: dict[tuple, dict[str, list[dict]]] = defaultdict(lambda: defaultdict(list))
    for r in rows:
        by_key[(r["scenario"], "all")][r["variant"]].append(r)
        if r["rainy"]:
            by_key[(r["scenario"], "rainy")][r["variant"]].append(r)

    for (scenario, subset), variants in by_key.items():
        n = len(variants["baseline"])
        label = "mọi chuyến" if subset == "all" else f"chuyến có ngày mưa (≥ {RAINY_DAY_HOURS} giờ mưa)"
        lines += ["", f"## {scenario} — {label} (n = {n})", "",
                  "| Biến thể | visits | rain_h | hazard (tổng) | di chuyển (phút) | feasible |",
                  "|---|---|---|---|---|---|"]
        for variant in present:
            rs = variants[variant]
            lines.append(
                f"| {variant} | {_mean(r['visits'] for r in rs):.2f} | {_mean(r['outdoor_rain_hours'] for r in rs):.3f}"
                f" | {sum(r['hazard_visits'] for r in rs)} | {_mean(r['travel_minutes'] for r in rs):.0f}"
                f" | {_mean(r['feasible'] for r in rs):.1%} |")
        pairs = [(v, "baseline") for v in present if v != "baseline"]
        if "llm" in present:
            pairs.append(("aware", "llm"))
        if n >= 2:
            lines += ["", "| So sánh | Chỉ số | Chênh lệch trung bình | CI 95% |", "|---|---|---|---|"]
            for variant, ref in pairs:
                for metric in ("outdoor_rain_hours", "hazard_visits", "visits"):
                    a = [r[metric] for r in variants[variant]]
                    b = [r[metric] for r in variants[ref]]
                    diff, lo, hi = paired_bootstrap(a, b, n_resamples=5000)
                    lines.append(f"| {variant} − {ref} | {metric} | {diff:+.3f} | [{lo:+.3f}, {hi:+.3f}] |")
            base = _mean(r["outdoor_rain_hours"] for r in variants["baseline"])
            if base > 0:
                for variant in (v for v in present if v != "baseline"):
                    change = _mean(r["outdoor_rain_hours"] for r in variants[variant]) / base - 1
                    verb = "giảm" if change < 0 else "tăng"
                    lines.append(f"\n{variant}: {verb} {abs(change):.0%} giờ ngoài trời khi mưa so với baseline.")

    no_forecast = sum(1 for r in rows if r["variant"] == "aware" and NONE in r["forecast_tier"].split(","))
    if no_forecast:
        lines += ["", f"**Lưu ý:** {no_forecast} chuyến thiếu dự báo lưu trữ ở ít nhất một ngày;"
                      " ngày đó aware chạy như baseline."]
    for variant in present:
        kinds = defaultdict(int)
        for r in rows:
            if r["variant"] == variant and not r["feasible"]:
                for k in r["violations"].split(";"):
                    kinds[k] += 1
        if kinds:
            lines += ["", f"Vi phạm khả thi của {variant} (số lịch có loại vi phạm): "
                      + ", ".join(f"{k}: {v}" for k, v in sorted(kinds.items()))]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Baseline LLM
# ---------------------------------------------------------------------------


def collect_llm(conn, cfg: ExperimentConfig, client, llm_dir: str | Path, max_trips: int | None = None,
                workers: int = 8, fetch_json: FetchJson | None = None) -> dict:
    """Gọi LLM lập lịch cho mọi chuyến của thực nghiệm, lưu câu trả lời vào
    `llm_dir/<số ngày>d/<ngày đầu>.json`. Chuyến đã có file thì bỏ qua (chạy lại an toàn).
    `client`: có `chat(messages, json_mode=True)` như `agent.llm.LLMClient`."""
    places = load_places_by_qid(conn, cfg.places)
    fetch_json = fetch_json or default_fetch_json(conn)
    forecast, observed = load_weather(fetch_json, [cfg.hotel] + [(p.lat, p.lon) for p in places],
                                      cfg.periods, cfg.lead_days)
    jobs = []
    for scenario, start, _ in iter_trips(cfg, forecast, observed, max_trips):
        path = llm_schedule_path(llm_dir, scenario.trip_days, start)
        if not path.exists():  # prompt dựng ngay vì tầng thời tiết đặt theo chuyến
            jobs.append((path, llm_prompt(places, constraint_for(cfg, start, scenario.trip_days), forecast)))

    def ask(job):
        path, prompt = job
        message = client.chat([{"role": "user", "content": prompt}], json_mode=True)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(message.content or "", encoding="utf-8")

    failed = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(ask, job): job[0] for job in jobs}
        for done, future in enumerate(as_completed(futures), 1):
            if future.exception() is not None:
                failed.append(f"{futures[future]}: {future.exception()}")
            if done % 50 == 0:
                print(f"  {done}/{len(jobs)}", flush=True)
    Path(llm_dir).mkdir(parents=True, exist_ok=True)
    (Path(llm_dir) / "meta.json").write_text(json.dumps(
        {"model": client.model, "provider": getattr(client, "provider", None),
         "reasoning_effort": getattr(client, "reasoning_effort", None)}), encoding="utf-8")
    return {"requested": len(jobs), "failed": failed}


def _llm_setup(conn, cfg: ExperimentConfig, start: date, trip_days: int):
    places = load_places_by_qid(conn, cfg.places)
    fetch_json = default_fetch_json(conn)
    days = [start + timedelta(days=i) for i in range(trip_days)]
    forecast, observed = load_weather(fetch_json, [cfg.hotel] + [(p.lat, p.lon) for p in places],
                                      [(days[0], days[-1])], cfg.lead_days)
    for day in days:
        forecast.set_tier(day, tier_for_lead(cfg.lead_days))
        observed.set_tier(day, HOURLY)
    return places, forecast, observed, constraint_for(cfg, start, trip_days)


def main() -> int:
    parser = argparse.ArgumentParser(prog="planner.experiment")
    sub = parser.add_subparsers(dest="command", required=True)
    p_run = sub.add_parser("run")
    p_run.add_argument("--config", default=DEFAULT_CONFIG)
    p_run.add_argument("--time-limit", type=float, default=1.0)
    p_run.add_argument("--max-trips", type=int, default=None, help="Giới hạn số chuyến mỗi kịch bản (chạy thử)")
    p_run.add_argument("--routing", choices=["estimate", "osrm"], default="estimate",
                       help="Nguồn thời gian đi; osrm cần `docker compose up -d osrm`")
    p_run.add_argument("--llm-dir", default=None, help="Thêm biến thể llm từ lịch do llm-collect lưu")
    p_collect = sub.add_parser("llm-collect")
    p_collect.add_argument("--config", default=DEFAULT_CONFIG)
    p_collect.add_argument("--model", default=None, help="Mặc định OPENAI_MODEL hoặc model của agent")
    p_collect.add_argument("--base-url", default=None,
                           help="Endpoint tương thích OpenAI; mặc định OPENAI_BASE_URL (Ollama: http://localhost:11434/v1)")
    p_collect.add_argument("--api-key-env", default=None,
                           help="Tên biến chứa key cho --base-url (vd GEMINI_API_KEY); mặc định OPENAI_API_KEY")
    p_collect.add_argument("--reasoning-effort", default="none")
    p_collect.add_argument("--workers", type=int, default=8, help="Ollama local: 1-2")
    p_collect.add_argument("--max-trips", type=int, default=None)
    p_collect.add_argument("--llm-dir", default=None,
                           help="Mặc định eval/llm_schedules/<model>_<reasoning-effort>")
    p_prompt = sub.add_parser("llm-prompt")
    p_prompt.add_argument("--date", required=True)
    p_prompt.add_argument("--trip-days", type=int, default=1)
    p_prompt.add_argument("--config", default=DEFAULT_CONFIG)
    p_score = sub.add_parser("llm-score")
    p_score.add_argument("schedule_json")
    p_score.add_argument("--trip-days", type=int, default=1)
    p_score.add_argument("--config", default=DEFAULT_CONFIG)
    args = parser.parse_args()

    from pipeline import db

    cfg = load_config(args.config)
    with db.connect() as conn:
        if args.command == "run":
            routing = None
            if args.routing == "osrm":
                from maps.osrm import OsrmClient

                routing = OsrmClient()
                if not routing.is_enabled:
                    raise SystemExit(routing.disabled_note)
            report, _ = run(conn, cfg, time_limit_s=args.time_limit, max_trips=args.max_trips,
                            maps_client=routing, llm_dir=args.llm_dir)
            print(report)
        elif args.command == "llm-collect":
            from agent.llm import llm_from_env, load_env_value

            client = llm_from_env(
                model=args.model, base_url=args.base_url, reasoning_effort=args.reasoning_effort, timeout=600.0,
                api_key=load_env_value(args.api_key_env) if args.api_key_env else None)
            if client is None:
                raise SystemExit("Chưa cấu hình LLM: đặt OPENAI_API_KEY, hoặc --base-url/OPENAI_BASE_URL tới Ollama.")
            safe_model = re.sub(r"[^\w.-]", "-", client.model)  # "qwen3.5:9b" không hợp lệ làm tên thư mục Windows
            llm_dir = args.llm_dir or f"eval/llm_schedules/{safe_model}_{args.reasoning_effort}"
            summary = collect_llm(conn, cfg, client, llm_dir, max_trips=args.max_trips, workers=args.workers)
            print(f"{llm_dir}: gọi {summary['requested']} lịch, lỗi {len(summary['failed'])};"
                  f" token vào {client.usage.input_tokens}, ra {client.usage.output_tokens}.")
            for line in summary["failed"]:
                print("  " + line)
        elif args.command == "llm-prompt":
            start = date.fromisoformat(args.date)
            places, forecast, _, constraint = _llm_setup(conn, cfg, start, args.trip_days)
            out = Path("eval/llm_schedules") / f"{start.isoformat()}.prompt.md"
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(llm_prompt(places, constraint, forecast), encoding="utf-8")
            print(f"Đã ghi {out}. Dán vào LLM, lưu JSON trả về thành {out.with_suffix('').with_suffix('.json')}")
        else:
            path = Path(args.schedule_json)
            start = date.fromisoformat(path.name.split(".")[0])
            places, forecast, observed, constraint = _llm_setup(conn, cfg, start, args.trip_days)
            by_id = {p.id: p for p in places}
            llm = schedule_from_json(path.read_text(encoding="utf-8"), by_id)
            solver = WeatherAwareScheduleOptimizer().optimize(places, constraint, forecast)
            for name, result in (("llm", llm), ("aware", solver)):
                metrics = evaluate_variant(result, by_id, constraint, observed)
                feas = check_feasibility(result, by_id, constraint)
                print(f"== {name}: {json.dumps(metrics, ensure_ascii=False)}")
                for v in feas.violations:
                    print(f"   [{v.kind}] {v.place_id}: {v.detail}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
