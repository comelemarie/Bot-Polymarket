"""Suivi de performance a partir de la base SQLite (PAPER, LIVE ou REPLAY).

  python -m analytics.performance_tracker [--db state/bot.db] [--outcomes-db state/bot.db]

Distingue le realise (sorties, reglements) de l'estime (positions ouvertes au meilleur bid).
Toute statistique est accompagnee de son effectif. En dessous de ~30 observations, les moyennes sont
du bruit; meme au-dela, une periode favorable ou un choix de seuils ajuste sur les memes donnees
peut produire un resultat positif sans avantage reel. Voir docs/VALIDATION.md.
"""
from __future__ import annotations

import argparse
import json
import math
from collections import defaultdict
from statistics import mean, stdev

from core.store import Store

MIN_N = 30


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return 0.0, 1.0
    p = k / n
    den = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / den
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return max(0.0, centre - half), min(1.0, centre + half)


def mean_ci(xs: list[float]) -> dict:
    n = len(xs)
    if n == 0:
        return {"n": 0}
    m = mean(xs)
    if n < 2:
        return {"n": n, "mean": m}
    half = 1.96 * stdev(xs) / math.sqrt(n)
    return {"n": n, "mean": m, "ci95": [m - half, m + half], "fiable": n >= MIN_N}


def max_drawdown(equity: list[float]) -> float:
    peak, mdd = -math.inf, 0.0
    for e in equity:
        peak = max(peak, e)
        if peak > 0:
            mdd = max(mdd, (peak - e) / peak)
    return mdd


def _bucket_delay(s: float | None) -> str:
    if s is None:
        return "inconnu"
    for limit, label in ((60, "<1 min"), (300, "1-5 min"), (1800, "5-30 min"), (7200, "30 min-2 h")):
        if s < limit:
            return label
    return ">2 h"


def report(db: Store, outcomes_db: Store | None = None) -> dict:
    out: dict = {}
    outcomes = {r["market_slug"]: r["winner"] for r in (outcomes_db or db).query("SELECT * FROM outcomes")}

    # --- PnL realise et couts ---
    exits = db.query("SELECT * FROM exits")
    entries = db.query("SELECT * FROM orders WHERE purpose='entry'")
    filled = [o for o in entries if o["status"] == "filled"]
    realized = sum(e["pnl_usdc"] for e in exits)
    fees = sum(o["fee_usdc"] for o in db.query("SELECT fee_usdc FROM orders WHERE status='filled'"))
    engaged = sum((o["filled_shares"] or 0) * (o["avg_price"] or 0) for o in filled)
    out["pnl"] = {
        "realise_net_usdc": realized,
        "frais_payes_usdc": fees,
        "realise_brut_usdc": realized + fees,
        "capital_engage_usdc": engaged,
        "rendement_sur_capital_engage": realized / engaged if engaged else None,
        "positions_ouvertes_cout_usdc": db.one("SELECT COALESCE(SUM(shares*avg_price),0) v FROM positions")["v"],
        "note": "Le PnL des positions ouvertes n'est pas realise et n'est pas inclus ci-dessus.",
    }
    eq = [r["equity"] for r in db.query("SELECT equity FROM equity ORDER BY ts")]
    out["drawdown_max"] = max_drawdown(eq) if eq else None

    # --- execution ---
    by_status = defaultdict(int)
    for o in entries:
        by_status[o["status"]] += 1
    aborted = sum(1 for d in db.query("SELECT details FROM decisions WHERE action='trade'")
                  if str(json.loads(d["details"] or "{}").get("execution", "")).startswith(("aborted", "blocked")))
    out["execution"] = {
        "ordres_entree": dict(by_status),
        "decisions_annulees_avant_envoi": aborted,
        "taux_execution": by_status["filled"] / len(entries) if entries else None,
        "slippage_vs_meilleur_prix_observe": mean_ci([(o["avg_price"] or 0) - o["observed_best_price"]
                                                      for o in filled if o["observed_best_price"] is not None]),
    }
    days = db.one("SELECT (MAX(created_at)-MIN(created_at))/86400.0 d FROM events")["d"] or 0
    out["frequence"] = {"trades": len(filled), "jours_observes": round(days, 2),
                        "trades_par_jour": len(filled) / days if days else None}

    # --- performance par trade, attribuee a la voie de detection, a la source, au delai, a la proba ---
    pnl_by_token = defaultdict(float)
    for e in exits:
        pnl_by_token[e["token_id"]] += e["pnl_usdc"]
    groups: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    for o in filled:
        if o["token_id"] not in pnl_by_token:
            continue  # position non soldee: pas de resultat realise
        ret = pnl_by_token[o["token_id"]] / max(1e-9, (o["filled_shares"] or 0) * (o["avg_price"] or 0))
        dec = db.one("SELECT * FROM decisions WHERE id=?", (o["decision_id"],))
        if dec is None:
            continue
        ev = db.one("SELECT * FROM events WHERE id=?", (dec["event_id"],))
        an = db.one("SELECT p_yes FROM analyses WHERE id=?", (dec["analysis_id"],)) if dec["analysis_id"] else None
        news_ids = json.loads(ev["news_ids"]) if ev else []
        news = db.one("SELECT source, published_at, received_at FROM news WHERE id=?", (news_ids[0],)) if news_ids else None
        ref = (news["published_at"] or news["received_at"]) if news else None
        groups["voie"][ev["kind"] if ev else "?"].append(ret)
        groups["source"][news["source"] if news else "?"].append(ret)
        groups["delai_reaction"][_bucket_delay(o["created_at"] - ref if ref else None)].append(ret)
        if an and an["p_yes"] is not None:
            groups["proba_predite"][f"{int(an['p_yes'] * 10) / 10:.1f}"].append(ret)
    out["rendement_par_trade_solde"] = {g: {k: mean_ci(v) for k, v in d.items()} for g, d in groups.items()}

    # --- qualite probabiliste: Brier du modele vs Brier du prix de marche, calibration ---
    model_sq, market_sq, bins = [], [], defaultdict(lambda: [0, 0, 0.0])
    for a in db.query("SELECT * FROM analyses WHERE valid=1 AND abstain=0 AND p_yes IS NOT NULL"):
        w = outcomes.get(a["market_slug"])
        if w is None:
            continue
        y = 1.0 if w == "YES" else 0.0
        model_sq.append((a["p_yes"] - y) ** 2)
        # Le premier instantane d'un evenement est celui du token YES (Bot.snapshot ecrit YES puis NO).
        snap = db.one("SELECT best_bid, best_ask FROM market_snapshots WHERE ref_id=? ORDER BY id LIMIT 1",
                      (a["event_id"],))
        if snap and snap["best_bid"] is not None and snap["best_ask"] is not None:
            market_sq.append(((snap["best_bid"] + snap["best_ask"]) / 2 - y) ** 2)
        b = min(9, int(a["p_yes"] * 10))
        bins[b][0] += 1
        bins[b][1] += int(y)
        bins[b][2] += a["p_yes"]
    out["brier"] = {
        "modele": mean(model_sq) if model_sq else None,
        "prix_du_marche_au_declenchement": mean(market_sq) if market_sq else None,
        "n": len(model_sq),
        "lecture": "Le modele n'apporte d'information que si son Brier est significativement inferieur a "
                   "celui du marche, sur un echantillon suffisant et hors des donnees de calibration.",
    }
    out["calibration"] = [
        {"tranche": f"{b / 10:.1f}-{(b + 1) / 10:.1f}", "n": n, "p_moyenne": s / n, "frequence_observee": k / n,
         "ic95_frequence": wilson(k, n)}
        for b, (n, k, s) in sorted(bins.items()) if n
    ]

    # --- qualite des abstentions et refus: qu'aurait rapporte l'achat refuse ? ---
    rej: dict[str, list[float]] = defaultdict(list)
    for d in db.query("SELECT * FROM decisions WHERE action='reject'"):
        w = outcomes.get(d["market_slug"])
        an = db.one("SELECT p_yes FROM analyses WHERE id=?", (d["analysis_id"],)) if d["analysis_id"] else None
        if w is None or an is None or an["p_yes"] is None:
            continue
        snaps = db.query("SELECT token_id, best_ask FROM market_snapshots WHERE ref_id=? ORDER BY id",
                         (f"decide:{d['event_id']}",))
        if len(snaps) < 2:
            continue
        yes_ask, no_ask = snaps[0]["best_ask"], snaps[1]["best_ask"]
        favored_yes = an["p_yes"] >= 0.5
        ask = yes_ask if favored_yes else no_ask
        if ask is None:
            continue
        payoff = 1.0 if (w == "YES") == favored_yes else 0.0
        kind = "abstention" if d["reason"].startswith("abstention") else "refus_risque_ou_avantage"
        rej[kind].append(payoff - ask)
    out["decisions_rejetees_gain_hypothetique_par_part"] = {
        k: mean_ci(v) for k, v in rej.items()} or "aucune issue connue pour les decisions rejetees"

    out["avertissements"] = [w for w in (
        f"Seulement {len(filled)} trade(s) execute(s): aucune conclusion de rentabilite possible."
        if len(filled) < MIN_N else None,
        f"Seulement {len(model_sq)} analyse(s) avec issue connue: calibration non significative."
        if len(model_sq) < MIN_N else None,
        "Resultats PAPER/REPLAY: execution simulee sans impact sur le carnet ni file d'attente (optimiste).",
    ) if w]
    return out


def main() -> None:
    p = argparse.ArgumentParser(description="Rapport de performance")
    p.add_argument("--db", default="state/bot.db")
    p.add_argument("--outcomes-db", default=None, help="base contenant la table outcomes (ex: base de collecte)")
    args = p.parse_args()
    db = Store(args.db)
    rep = report(db, Store(args.outcomes_db) if args.outcomes_db else None)
    print(json.dumps(rep, indent=2, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
