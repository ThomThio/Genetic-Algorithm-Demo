"""Serializes discovered strategies to JSON, in the shape the future
Supabase table is expected to mirror (one row per discovered strategy).
"""
import json
import os
import uuid
from datetime import datetime, timezone
from typing import List

from .fitness import OOS_MIN_TRADES


def build_record(*, symbol: str, data_source: str, train_range, test_range,
                  evaluated, oos_edge, resolved_conditions,
                  interpretation: str, generation_found: int, ga_params: dict) -> dict:
    spec = evaluated.spec
    return {
        'strategy_id': str(uuid.uuid4()),
        'created_at': datetime.now(timezone.utc).isoformat(),
        'direction': 'long',
        'data_source': data_source,
        'universe_slice': {
            'symbol': symbol,
            'train_range': [str(train_range[0]), str(train_range[1])],
            'test_range': [str(test_range[0]), str(test_range[1])],
        },
        'entry_rule': {
            'logic': 'AND',
            'conditions': [
                {'key': c.key, 'name': c.name, 'params': c.params, 'archetype': c.archetype}
                for c in resolved_conditions
            ],
            'rule_text': ' AND '.join(c.label() for c in resolved_conditions),
        },
        'risk_model': {
            'stop_atr_mult': spec.stop_atr_mult,
            'target_R': spec.target_R,
            'max_hold_bars': spec.max_hold_bars,
            'entry_fill': 'next_bar_open',
        },
        'performance': {
            'in_sample': evaluated.edge.to_dict(),
            'out_of_sample': oos_edge.to_dict(),
        },
        'validated_out_of_sample': bool(oos_edge.edge_score > 0 and oos_edge.n_trades >= OOS_MIN_TRADES),
        'edge_score': oos_edge.edge_score if oos_edge.n_trades >= OOS_MIN_TRADES else evaluated.edge.edge_score,
        'interpretation': interpretation,
        'ga_meta': {
            'generation_found': generation_found,
            **ga_params,
            'genome_signature': spec.signature(),
        },
    }


def write_records(records: List[dict], out_dir: str, run_name: str) -> str:
    os.makedirs(out_dir, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    path = os.path.join(out_dir, f'discovery_{run_name}_{ts}.json')
    with open(path, 'w') as f:
        json.dump(records, f, indent=2, default=str)

    latest_path = os.path.join(out_dir, 'latest.json')
    with open(latest_path, 'w') as f:
        json.dump(records, f, indent=2, default=str)

    return path
