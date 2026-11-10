#!/usr/bin/env python3
"""Read-only 1 s sampler of awdl0 bytes and key AWDL counters.

  python3 -I mtk-sampler.py <label> <seconds>   -> ./snapshots/<time>-<label>.csv
  python3 -I mtk-sampler.py report <file.csv>    -> transfer windows, rates, errors

No sudo; stores no addresses.
"""
import csv, plistlib, re, subprocess, sys, time

FIELDS = ['MTKAWDLTXDataAcked', 'MTKAWDLTXDataFailed', 'MTKAWDLDataTxDeferred', 'MTKAWDLTXNoPeer',
          'MTKAWDLDataTxExpired', 'MTKAWDLDataTxNotReady', 'MTKAWDLDataRxDecoded', 'MTKAWDLROCScanBlocked',
          'MTKAWDLTXPeer0RateMbps', 'MTKAWDLTXPeer1RateMbps', 'MTKAWDLTXPeer2RateMbps', 'MTKAWDLSyncEnabled']


def props():
    out = {}
    for cls in ('AirportMtkWlan', 'AirportMtkAwdlInterface'):
        try:
            raw = subprocess.run(['ioreg', '-a', '-r', '-c', cls], capture_output=True, timeout=10).stdout
            for root in plistlib.loads(raw) if raw else []:
                for k in FIELDS:
                    if k in root and k not in out:
                        v = root[k]
                        out[k] = int(v) if isinstance(v, bool) else v
        except Exception:
            pass
    return out


def awdl_bytes():
    r = subprocess.run(['netstat', '-I', 'awdl0', '-b'], capture_output=True, text=True).stdout
    for line in r.splitlines()[1:]:
        c = line.split()
        if len(c) >= 11 and c[0] == 'awdl0' and c[2].startswith('<Link'):
            return int(c[6]), int(c[9])
    return None, None


def sample(label, seconds):
    path = f'snapshots/{time.strftime("%Y%m%d-%H%M%S")}-{label}.csv'
    import os; os.makedirs('snapshots', exist_ok=True)
    with open(path, 'w', newline='') as f:
        w = csv.writer(f)
        w.writerow(['t', 'ibytes', 'obytes'] + FIELDS)
        t0 = time.time()
        while time.time() - t0 < seconds:
            ib, ob = awdl_bytes()
            p = props()
            w.writerow([round(time.time() - t0, 2), ib, ob] + [p.get(k, '') for k in FIELDS])
            f.flush()
            time.sleep(max(0, 1 - (time.time() - t0) % 1))
    print(path)


def report(path):
    # Rows where awdl0 was absent (interface down) carry no byte counters;
    # dropping them avoids treating a missing value as zero.
    rows = [r for r in csv.DictReader(open(path)) if r['ibytes'] not in ('', None)]
    num = lambda r, k: int(r[k]) if r[k] not in ('', None) else 0
    for direction, key in (('Mac->iPhone (TX)', 'obytes'), ('iPhone->Mac (RX)', 'ibytes')):
        active = [i for i in range(1, len(rows)) if num(rows[i], key) - num(rows[i - 1], key) > 20000]
        if not active:
            print(f'{direction}: no transfer seen'); continue
        # group into bursts separated by >10 s of inactivity
        bursts, cur = [], [active[0]]
        for i in active[1:]:
            if float(rows[i]['t']) - float(rows[cur[-1]]['t']) > 10: bursts.append(cur); cur = [i]
            else: cur.append(i)
        bursts.append(cur)
        for b in bursts:
            a, z = b[0] - 1, b[-1]
            dt = float(rows[z]['t']) - float(rows[a]['t'])
            nbytes = num(rows[z], key) - num(rows[a], key)
            if nbytes < 500000: continue
            acked = num(rows[z], 'MTKAWDLTXDataAcked') - num(rows[a], 'MTKAWDLTXDataAcked')
            failed = num(rows[z], 'MTKAWDLTXDataFailed') - num(rows[a], 'MTKAWDLTXDataFailed')
            rates = [num(rows[i], 'MTKAWDLTXPeer0RateMbps') for i in range(a, z + 1)]
            peak = max(num(rows[i], key) - num(rows[i - 1], key) for i in range(a + 1, z + 1))
            print(f'{direction}: t={rows[a]["t"]}..{rows[z]["t"]} s  {nbytes / 1e6:.2f} MB in {dt:.1f} s = '
                  f'{nbytes / dt / 1e6:.2f} MB/s (peak 1 s {peak / 1e6:.2f} MB/s)')
            if acked + failed:
                print(f'   TX unicast fail {100 * failed / (acked + failed):.2f}% ({failed}/{acked + failed})')
            print(f'   peer0 rate Mbps: min {min(rates)} max {max(rates)} mode {max(set(rates), key=rates.count)}')
            for k in ('MTKAWDLDataTxDeferred', 'MTKAWDLTXNoPeer', 'MTKAWDLDataTxExpired', 'MTKAWDLDataTxNotReady', 'MTKAWDLROCScanBlocked'):
                print(f'   {k} {num(rows[z], k) - num(rows[a], k):+d}')


if __name__ == '__main__':
    if len(sys.argv) == 3 and sys.argv[1] == 'report':
        report(sys.argv[2])
    elif len(sys.argv) == 3:
        sample(sys.argv[1], float(sys.argv[2]))
    else:
        sys.exit(__doc__)
