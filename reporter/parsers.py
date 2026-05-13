"""Parsers for memtier benchmark logs and CloudWatch metrics CSVs."""

import re
from io import StringIO

import pandas as pd


def parse_memtier_logs(log_content):
    """Extract time-series data from memtier benchmark CloudWatch log export.

    Returns a DataFrame with columns: Timestamp, Ops/sec, Latency (ms), Bandwidth_KBs.
    """
    # Matches: [2026-03-07T10:13:12.212000] [stream/name] rest-of-message
    _HEADER = re.compile(r'^\[([\d\-T:\.]+)\] \[([^\]]+)\] (.*)', re.DOTALL)

    records = []

    for line in log_content.split('\n'):
        header = _HEADER.match(line)
        if not header:
            continue
        timestamp = header.group(1)
        stream = header.group(2)
        rest = header.group(3)

        if 'ops/sec' not in rest.lower() or 'latency' not in rest.lower():
            continue

        ops_match = re.search(r'\(avg:\s*([\d\.]+)\)\s*ops/sec', rest)
        if not ops_match:
            ops_match = re.search(r'([\d\.]+)\s*ops/sec', rest)
        ops_sec = float(ops_match.group(1)) if ops_match else None

        lat_match = re.search(r'\(avg:\s*([\d\.]+)\)\s*msec latency', rest)
        if not lat_match:
            lat_match = re.search(r'([\d\.]+)\s*msec latency', rest)
        latency = float(lat_match.group(1)) if lat_match else None

        bw_match = re.search(r'\(avg:\s*([\d\.]+)(KB|MB)/sec\)', rest)
        if bw_match:
            bw_val = float(bw_match.group(1))
            bw_kbs = bw_val * 1024 if bw_match.group(2) == 'MB' else bw_val
        else:
            bw_kbs = None

        if ops_sec is not None and latency is not None:
            records.append({
                'Timestamp':      pd.to_datetime(timestamp, format='ISO8601'),
                'Stream':         stream,
                'Ops/sec':        ops_sec,
                'Latency (ms)':   latency,
                'Bandwidth_KBs':  bw_kbs,
            })

    if not records:
        return pd.DataFrame()

    df = pd.DataFrame(records)
    if not df.empty:
        df['Timestamp'] = pd.to_datetime(df['Timestamp'])
        df = df.sort_values(['Timestamp', 'Stream']).reset_index(drop=True)
    return df

def parse_memtier_extra_stats(log_content):
    """Extract scalar statistics and OOM events from raw memtier log.
    Returns a dict with:
      first_message_ts   – datetime of the very first memtier log message
      last_message_ts    – datetime of the very last memtier log message
      first_eviction_ts  – datetime of first -OOM line
      oom_df             – DataFrame [Timestamp, OOM_events] using the event log timestamp
    """
    ts_pat = re.compile(r'^\[([\d\-T:\.]+)\]')
    first_message_ts = None
    last_message_ts = None
    first_eviction_ts = None
    oom_events = []

    for line in log_content.split('\n'):
        m = ts_pat.match(line)
        if not m:
            continue
        try:
            ts = pd.to_datetime(m.group(1), format='ISO8601')
        except Exception:
            continue

        if first_message_ts is None or ts < first_message_ts:
            first_message_ts = ts
        if last_message_ts is None or ts > last_message_ts:
            last_message_ts = ts
        if '-OOM command not allowed' in line:
            if first_eviction_ts is None or ts < first_eviction_ts:
                first_eviction_ts = ts
            oom_events.append(ts)

    if oom_events:
        oom_df = pd.DataFrame({'Timestamp': pd.to_datetime(oom_events), 'OOM_events': 1})
        if oom_df['Timestamp'].dt.tz is not None:
            oom_df['Timestamp'] = oom_df['Timestamp'].dt.tz_localize(None)
        oom_df = oom_df.groupby('Timestamp', as_index=False)['OOM_events'].sum()
    else:
        oom_df = pd.DataFrame(columns=['Timestamp', 'OOM_events'])

    return {
        'first_message_ts': first_message_ts,
        'last_message_ts': last_message_ts,
        'first_eviction_ts': first_eviction_ts,
        'oom_df': oom_df,
    }


def parse_metrics_csv(csv_content):
    """Parse a CloudWatch metrics CSV into a DataFrame, normalising timestamps to tz-naive UTC."""
    df = pd.read_csv(StringIO(csv_content))

    required_columns = [
        "Timestamp", "Namespace", "MetricName", "Stat", "Value", "Unit", "Dimensions",
    ]
    missing_columns = [col for col in required_columns if col not in df.columns]
    if missing_columns:
        raise ValueError(f"Metrics CSV is missing required columns: {', '.join(missing_columns)}")

    if not df.empty:
        df['Timestamp'] = pd.to_datetime(df['Timestamp'], format='ISO8601', utc=True)
        df['Timestamp'] = df['Timestamp'].dt.tz_localize(None)
        df = df.sort_values(['Timestamp', 'Namespace', 'MetricName', 'Stat', 'Dimensions']).reset_index(drop=True)
    return df
