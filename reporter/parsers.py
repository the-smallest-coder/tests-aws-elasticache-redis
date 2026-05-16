"""Parsers for memtier benchmark logs and CloudWatch metrics CSVs."""

import json
import re
from io import StringIO

import pandas as pd


def _event_timestamp_ms_to_datetime(timestamp):
    if timestamp is None:
        return None
    try:
        ts = pd.to_datetime(int(timestamp), unit='ms', utc=True)
    except Exception:
        return None
    return ts.tz_convert('UTC').tz_localize(None)


def _iter_cloudwatch_memtier_lines(log_content, source_stream=None):
    """Yield (absolute_timestamp, stream, message, raw_line) from raw JSONL or legacy text exports."""
    # Legacy export format: [2026-03-07T10:13:12.212000] [stream/name] rest-of-message
    legacy_header = re.compile(r'^\[([\d\-T:\.]+)\] \[([^\]]+)\] (.*)', re.DOTALL)

    for raw_line in log_content.splitlines():
        if not raw_line:
            continue

        try:
            event = json.loads(raw_line)
        except json.JSONDecodeError:
            event = None

        if isinstance(event, dict) and 'timestamp' in event and 'message' in event:
            ts = _event_timestamp_ms_to_datetime(event.get('timestamp'))
            if ts is None:
                continue
            yield ts, source_stream or event.get('logStreamName', 'memtier'), str(event.get('message', '')), raw_line
            continue

        header = legacy_header.match(raw_line)
        if not header:
            continue
        try:
            ts = pd.to_datetime(header.group(1), format='ISO8601')
        except Exception:
            continue
        if getattr(ts, 'tzinfo', None) is not None:
            ts = ts.tz_convert('UTC').tz_localize(None)
        yield ts, header.group(2), header.group(3), raw_line


def parse_memtier_logs(log_content, source_stream=None):
    """Extract time-series data from memtier benchmark CloudWatch log export.

    Returns a DataFrame with columns: Timestamp, Ops/sec, Latency (ms), Bandwidth_KBs.
    """
    records = []

    for timestamp, stream, message, _raw_line in _iter_cloudwatch_memtier_lines(log_content, source_stream):
        if 'ops/sec' not in message.lower() or 'latency' not in message.lower():
            continue

        ops_match = re.search(r'\(avg:\s*([\d\.]+)\)\s*ops/sec', message)
        if not ops_match:
            ops_match = re.search(r'([\d\.]+)\s*ops/sec', message)
        ops_sec = float(ops_match.group(1)) if ops_match else None

        lat_match = re.search(r'\(avg:\s*([\d\.]+)\)\s*msec latency', message)
        if not lat_match:
            lat_match = re.search(r'([\d\.]+)\s*msec latency', message)
        latency = float(lat_match.group(1)) if lat_match else None

        bw_match = re.search(r'\(avg:\s*([\d\.]+)(KB|MB)/sec\)', message)
        if bw_match:
            bw_val = float(bw_match.group(1))
            bw_kbs = bw_val * 1024 if bw_match.group(2) == 'MB' else bw_val
        else:
            bw_kbs = None

        if ops_sec is not None and latency is not None:
            records.append({
                'Timestamp':      timestamp,
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

def parse_memtier_final_totals(log_content, source_stream=None):
    """Extract the last per-stream memtier ``Totals`` row, if one exists."""
    totals = []
    totals_re = re.compile(
        r'^Totals\s+([\d.]+)\s+[\d.]+\s+[\d.]+\s+([\d.]+)\s+'
        r'[\d.]+\s+[\d.]+\s+[\d.]+\s+([\d.]+)\s*$'
    )
    for timestamp, stream, message, _raw_line in _iter_cloudwatch_memtier_lines(log_content, source_stream):
        match = totals_re.match(message)
        if match:
            totals.append({
                'Timestamp': timestamp,
                'Stream': stream,
                'Ops/sec': float(match.group(1)),
                'Latency (ms)': float(match.group(2)),
                'Bandwidth_KBs': float(match.group(3)),
            })
    if not totals:
        return pd.DataFrame()
    return pd.DataFrame(totals).sort_values(['Stream', 'Timestamp']).groupby('Stream', as_index=False).tail(1)


def parse_memtier_extra_stats(log_content, source_stream=None):
    """Extract scalar statistics and OOM events from raw memtier log.
    Returns a dict with:
      first_message_ts   – datetime of the very first memtier log message
      last_message_ts    – datetime of the very last memtier log message
      first_eviction_ts  – datetime of first -OOM line
      oom_df             – DataFrame [Timestamp, OOM_events] using the event log timestamp
    """
    first_message_ts = None
    last_message_ts = None
    first_eviction_ts = None
    oom_events = []

    for ts, _stream, message, raw_line in _iter_cloudwatch_memtier_lines(log_content, source_stream):
        if first_message_ts is None or ts < first_message_ts:
            first_message_ts = ts
        if last_message_ts is None or ts > last_message_ts:
            last_message_ts = ts
        if '-OOM command not allowed' in message or '-OOM command not allowed' in raw_line:
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
