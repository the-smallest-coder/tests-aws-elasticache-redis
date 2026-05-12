"""Parsers for memtier benchmark logs and CloudWatch metrics CSVs."""

import re
from io import StringIO

import pandas as pd


def parse_memtier_logs(log_content):
    """Extract time-series data from memtier benchmark CloudWatch log export.

    CloudWatch batches all per-second memtier progress lines into a single log
    event, so only the *first* line in each batch carries a ``[CW_TS] [stream]``
    prefix.  Continuation lines carry no prefix at all.  We therefore track the
    last-seen (stream, CW ingestion timestamp) and propagate it to continuation
    lines.  The actual wall-clock time is reconstructed per-stream by computing:

        benchmark_start = max(CW_ingest_ts) - max(N_secs)
        actual_ts(N)    = benchmark_start + N_secs

    Returns a DataFrame with columns: Timestamp, Ops/sec, Latency (ms), Bandwidth_KBs.
    """
    # ------------------------------------------------------------------ #
    #  Phase 1 – collect raw records with (stream, cw_ts, n_secs, values) #
    # ------------------------------------------------------------------ #
    # Matches: [2026-03-07T10:13:12.212000] [stream/name] rest-of-message
    _HEADER = re.compile(r'^\[([\d\-T:\.]+)\] \[([^\]]+)\] (.*)', re.DOTALL)
    # Matches the elapsed-seconds counter: "N secs]" or ", N secs]"
    _SECS = re.compile(r',\s*(\d+)\s*secs\]')

    raw = []
    curr_cw_ts = None
    curr_stream = None

    for line in log_content.splitlines():
        header = _HEADER.match(line)
        if header:
            curr_cw_ts  = header.group(1)
            curr_stream = header.group(2)
            rest        = header.group(3)
        else:
            rest = line   # continuation line — reuse last stream/ts

        if curr_cw_ts is None or curr_stream is None:
            continue
        if 'ops/sec' not in rest.lower() or 'latency' not in rest.lower():
            continue

        secs_match = _SECS.search(rest)
        if not secs_match:
            continue
        n_secs = int(secs_match.group(1))

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
            raw.append({
                'Stream':         curr_stream,
                'CW_TS':          curr_cw_ts,
                'N_secs':         n_secs,
                'Ops/sec':        ops_sec,
                'Latency (ms)':   latency,
                'Bandwidth_KBs':  bw_kbs,
            })

    if not raw:
        return pd.DataFrame()

    # ------------------------------------------------------------------ #
    #  Phase 2 – reconstruct wall-clock timestamps per stream             #
    # ------------------------------------------------------------------ #
    df_raw = pd.DataFrame(raw)
    df_raw['CW_TS'] = pd.to_datetime(df_raw['CW_TS'], format='ISO8601')

    records = []
    for stream, grp in df_raw.groupby('Stream'):
        max_cw_ts      = grp['CW_TS'].max()
        max_n_secs     = grp['N_secs'].max()
        benchmark_start = max_cw_ts - pd.Timedelta(seconds=int(max_n_secs))
        for _, row in grp.iterrows():
            records.append({
                'Timestamp':      benchmark_start + pd.Timedelta(seconds=int(row['N_secs'])),
                'Ops/sec':        row['Ops/sec'],
                'Latency (ms)':   row['Latency (ms)'],
                'Bandwidth_KBs':  row['Bandwidth_KBs'],
            })

    df = pd.DataFrame(records)
    if not df.empty:
        df['Timestamp'] = pd.to_datetime(df['Timestamp'])
    return df


def parse_memtier_extra_stats(log_content):
    """Extract scalar statistics and per-minute OOM series from raw memtier log.

    Returns a dict with:
      process_start_ts   – datetime of the very first CloudWatch log entry
      first_eviction_ts  – datetime of first -OOM line
      oom_df             – DataFrame [Timestamp, OOM_per_min] (1-min buckets)
    """
    ts_pat = re.compile(r'^\[([\d\-T:\.]+)\]')
    last_ts = None
    process_start_ts = None
    first_eviction_ts = None
    oom_events = []

    for line in log_content.splitlines():
        m = ts_pat.match(line)
        if m:
            try:
                last_ts = pd.to_datetime(m.group(1), format='ISO8601')
                if process_start_ts is None:
                    process_start_ts = last_ts
            except Exception:
                pass
        if '-OOM command not allowed' in line and last_ts is not None:
            if first_eviction_ts is None:
                first_eviction_ts = last_ts
            oom_events.append(last_ts)

    if oom_events:
        oom_series = pd.Series([1] * len(oom_events), index=pd.DatetimeIndex(oom_events))
        oom_df = oom_series.resample('1min').sum().rename('OOM_per_min').reset_index()
        oom_df.columns = ['Timestamp', 'OOM_per_min']
        if oom_df['Timestamp'].dt.tz is not None:
            oom_df['Timestamp'] = oom_df['Timestamp'].dt.tz_localize(None)
    else:
        oom_df = pd.DataFrame(columns=['Timestamp', 'OOM_per_min'])

    return {
        'process_start_ts': process_start_ts,
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
    return df
