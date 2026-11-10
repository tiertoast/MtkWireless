#!/usr/bin/env python3
"""Read-only snapshot of MtkWireless counters, for before/after comparisons.

  python3 -I mtk-snapshot.py snap  <label>          -> writes ./snapshots/<time>-<label>.json
  python3 -I mtk-snapshot.py diff  <a.json> <b.json> -> prints deltas and derived metrics

No sudo, no writes outside ./snapshots, no MAC/BSSID/SSID/address values stored.
"""
import json, os, plistlib, re, subprocess, sys, time

REDACT = re.compile(r'(MAC|Mac|BSSID|Bssid|SSID|Ssid|Address|Addr|A1|A2|A3|Serial|Peer[A-Z]?Address)')
KEYS_OF_INTEREST = [
    # AWDL data path
    'MTKAWDLTXDataAcked', 'MTKAWDLTXDataFailed', 'MTKAWDLDataTxSubmitted', 'MTKAWDLDataTxDeferred',
    'MTKAWDLDataTxExpired', 'MTKAWDLTXNoPeer', 'MTKAWDLDataTxNotReady', 'MTKAWDLDataRxDecoded',
    'MTKAWDLNativeRxPackets', 'MTKAWDLNativeTxPackets', 'MTKAWDLDataRxUnknownPeer',
    # discovery / TLV4
    'MTKAWDLActionReceived', 'MTKAWDLActionHostAccepted', 'MTKAWDLActionHostRejected', 'MTKAWDLActionInvalid',
    # radio scheduling
    'MTKAWDLROCSubmitted', 'MTKAWDLROCGrants', 'MTKAWDLROCRejected', 'MTKAWDLROCScanBlocked', 'MTKAWDLScanPreempted',
    # BA
    'MTKAWDLADDBARequests', 'MTKAWDLADDBAAccepted', 'MTKAWDLRxBAStarted',
    # lifecycle
    'MTKAWDLSyncRequests', 'MTKAWDLSyncFailures', 'MTKAWDLServiceTicks',
    # D1-LIFECYCLE recovery ticket (absent on other builds)
    'MTKAWDLSyncRecoveryArmed', 'MTKAWDLSyncRecoveryRetries', 'MTKAWDLSyncRecoveryCompleted',
    'MTKAWDLSyncRecoveryStale', 'MTKAWDLSyncRecoveryExpired',
    # 1.0.13 ready gate (absent on 1.0.12 builds)
    'MTKAWDLReadyGateChecks', 'MTKAWDLReadyGateCompletions', 'MTKAWDLReadyGateBypass', 'MTKAWDLReadyGateWaitedUS',
]
STATE_KEYS = [
    'MTKAWDLEnabled', 'MTKAWDLSyncEnabled', 'MTKAWDLSyncTransition', 'MTKAWDLSyncRequests', 'MTKAWDLSyncFailures',
    'MTKAWDLSyncRecoveryStage', 'MTKAWDLNativeParentSelected', 'MTKAWDLSelectedClockValidated', 'MTKAWDLROCLastChannel',
    'MTKAWDLTXPeer0RateMbps', 'MTKAWDLTXPeer1RateMbps', 'MTKAWDLTXPeer2RateMbps', 'MTKAWDLReadyGate',
    'MTKWiFiJoin.MTKWiFiJoinState', 'MTKWiFiJoin.MTKWiFiJoinCompat', 'MTKWiFiJoin.MTKWiFiJoinRunBw',
    'MTKWiFiJoin.MTKWiFiJoinUpperAuth', 'IOLinkSpeed', 'IOModel',
]


def run(*cmd):
    try:
        return subprocess.run(cmd, capture_output=True, timeout=60).stdout
    except Exception:
        return b''


def walk(node, out, cls_path=''):
    if isinstance(node, dict):
        cls = node.get('IOObjectClass', '')
        for k, v in node.items():
            if k == 'IORegistryEntryChildren':
                for child in v:
                    walk(child, out, cls)
                continue
            if not (k.startswith('MTK') or k in ('IOLinkSpeed', 'IOModel', 'CFBundleIdentifier')):
                continue
            put(out, k, v, cls)


def put(out, k, v, cls):
    if REDACT.search(k):
        return
    if isinstance(v, dict):
        for kk, vv in v.items():
            put(out, f'{k}.{kk}', vv, cls)
        return
    if isinstance(v, (bytes, bytearray)):
        return  # blobs may carry addresses; skip
    if isinstance(v, int) and v >= 1 << 63:
        v -= 1 << 64  # ioreg prints signed values as unsigned 64-bit
    if k in out and out[k] != v:
        k = f'{cls}:{k}'
    out[k] = v


def snapshot(label):
    props = {}
    # `ioreg -r -c` returns full properties only for the matched node, so
    # query each class that publishes counters.
    for cls in ('AirportMtkWlan', 'AirportMtkAwdlInterface'):
        raw = run('ioreg', '-a', '-r', '-c', cls)
        if raw:
            for root in plistlib.loads(raw):
                root.pop('IORegistryEntryChildren', None)
                walk(root, props)
    kexts = {}
    for line in run('kmutil', 'showloaded', '--list-only').decode(errors='replace').splitlines():
        m = re.search(r'(com\.dinovn\.\S+|com\.nullmoth\.NVRM)\s+\(([^)]+)\)\s+([0-9A-F-]{36})', line)
        if m:
            kexts[m.group(1)] = {'version': m.group(2), 'uuid': m.group(3)}
    boot = run('sysctl', '-n', 'kern.boottime').decode()
    m = re.search(r'sec = (\d+)', boot)
    uptime = int(time.time()) - int(m.group(1)) if m else None
    awdl_flags = run('ifconfig', 'awdl0').decode(errors='replace').splitlines()[:1]
    bytes_io = None
    for line in run('netstat', '-I', 'awdl0', '-b').decode(errors='replace').splitlines()[1:]:
        # Name Mtu Network Address Ipkts Ierrs Ibytes Opkts Oerrs Obytes Coll
        cols = line.split()
        if len(cols) >= 11 and cols[0] == 'awdl0' and cols[2].startswith('<Link'):
            try:
                bytes_io = {'ipkts': int(cols[4]), 'ibytes': int(cols[6]), 'opkts': int(cols[7]), 'obytes': int(cols[9])}
            except ValueError:
                pass
    data = {
        'label': label, 'time': time.strftime('%Y-%m-%dT%H:%M:%S'), 'epoch': time.time(), 'uptime_s': uptime,
        'os': run('sw_vers', '-buildVersion').decode().strip(), 'kexts': kexts,
        'boot_args': run('nvram', 'boot-args').decode(errors='replace').strip().split('\t')[-1],
        'awdl0_flags': awdl_flags, 'awdl0_netstat': bytes_io, 'props': props,
    }
    os.makedirs('snapshots', exist_ok=True)
    path = os.path.join('snapshots', time.strftime('%Y%m%d-%H%M%S') + f'-{re.sub(r"[^A-Za-z0-9_.-]", "_", label)}.json')
    with open(path, 'w') as f:
        json.dump(data, f, indent=1, sort_keys=True)
    print(path)
    summary(data)


def summary(d):
    p = d['props']
    print(f"  uptime {d['uptime_s']} s · build {d['os']}")
    for k, v in d['kexts'].items():
        print(f'  {k} {v["version"]} {v["uuid"]}')
    for k in STATE_KEYS:
        if k in p:
            print(f'  {k} = {p[k]}')


def diff(a_path, b_path):
    a, b = (json.load(open(x)) for x in (a_path, b_path))
    if a['kexts'] != b['kexts']:
        print('!! loaded kexts differ between snapshots (reboot or reload?)')
    if b['uptime_s'] is not None and a['uptime_s'] is not None and b['uptime_s'] < a['uptime_s']:
        print('!! uptime went backwards: the snapshots are from different boots; deltas are meaningless')
    dt = b['epoch'] - a['epoch']
    print(f'{a["label"]} -> {b["label"]}: {dt:.1f} s')
    pa, pb = a['props'], b['props']
    for k in KEYS_OF_INTEREST:
        if k in pb and isinstance(pb[k], int):
            print(f'  {k:32s} {pb[k] - pa.get(k, 0):+d}')
    acked = pb.get('MTKAWDLTXDataAcked', 0) - pa.get('MTKAWDLTXDataAcked', 0)
    failed = pb.get('MTKAWDLTXDataFailed', 0) - pa.get('MTKAWDLTXDataFailed', 0)
    if acked + failed:
        print(f'  => unicast TX failure rate {100.0 * failed / (acked + failed):.2f}% ({failed}/{acked + failed})')
    nopeer = pb.get('MTKAWDLTXNoPeer', 0) - pa.get('MTKAWDLTXNoPeer', 0)
    deferred = pb.get('MTKAWDLDataTxDeferred', 0) - pa.get('MTKAWDLDataTxDeferred', 0)
    if deferred:
        print(f'  => NoPeer/Deferred {nopeer}/{deferred}')
    na, nb = a.get('awdl0_netstat'), b.get('awdl0_netstat')
    if na and nb and dt > 0:
        print(f"  => awdl0 out {(nb['obytes'] - na['obytes']) / dt / 1e3:.1f} kB/s · in {(nb['ibytes'] - na['ibytes']) / dt / 1e3:.1f} kB/s (avg over window)")
    print('  state after:')
    summary(b)


if __name__ == '__main__':
    if len(sys.argv) == 3 and sys.argv[1] == 'snap':
        snapshot(sys.argv[2])
    elif len(sys.argv) == 4 and sys.argv[1] == 'diff':
        diff(sys.argv[2], sys.argv[3])
    else:
        sys.exit(__doc__)
