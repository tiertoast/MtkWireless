# MT7922 AirDrop on a desktop Hackintosh: lifecycle, rate and country code

Field notes from 9–10 November 2026 for branch `archive/mt7922-airdrop-it`. Each build changes one thing on top of the previous one and was tested on hardware before the next one was written. Labels: **CONFIRMED** means measured on hardware or read in the code. **PROBABLE** means consistent with the data but not isolated. **TO VERIFY** means an open question.

## Machine

| | |
|---|---|
| Board | ASUS ROG STRIX B650E-F, BIOS 4004, Ryzen 7 9800X3D |
| GPU | RTX 3090 FE with NVRM/NullMoth 2.8.1 (BAR1 is remapped about 21–23 s after boot) |
| macOS | Tahoe 26.7.2 (25G332), OpenCore 1.0.9 DEBUG booted from USB |
| Wi-Fi / BT | MediaTek MT7922 (RZ616), PCIe `14c3:0616` subsystem `105b:e0cd`; BT on USB `0489:e0e2` |
| Peer | iPhone on iOS 26/27 |
| AP | 5 GHz, first on channel 120 (DFS, 80 MHz), then on 44 (80 MHz) |

## Build line

All builds start from Release 1.0.12 (`3bb3ced`). `Bluetooth` stays at the same build throughout.

| Build | Commit | Change |
|---|---|---|
| D1 | `ea58fc0` + `23b873f` | Codex "D1 lifecycle": arm a single retry ticket (`SyncRecovery`) when a sync ON is refused because the HAL is stopping. Includes the TLV4 tail fix `f895f0c`. |
| L2 | `e4c0bcc` | **Accept and defer**. When a sync ON is refused only because the HAL is stopping, the driver returns success and finishes the enable from the timer. |
| L3 | `2fbd151` | **Follow the deferred ON into the new HAL epoch**. The ticket is re-armed up to 8 times, against the same absolute deadline. |
| L4 | `61c5f40` | Structural TLV4 trailer rule (any 2-byte tail on the exact `[15,1,0,3,ff,ff]` + 16×2 shape) and rate-ladder thresholds 16/32/64. |
| L5 | `0416341` | Codex TX-EFFICIENCY (fresh rendezvous per frame, commit under the grant) plus a 4 ms end-of-window guard (`amtk_awdl_tx_guard_us`, 0–20000). |
| L6 | `99c0cfb` | **Country and locale**. Firmware `"00"` falls back to the airportd geolocation. The locale is ETSI for EU/EEA, CH and GB, and FCC otherwise, as before. |
| L7 | `2d7a531` | **Country from the AP's 802.11d Country element**: the boot-arg is no longer needed. |

The L4 ideas also reached upstream independently through PR #5 (`3793ade`, `302093a`), which shipped in 1.0.13.

## Findings

### 1. Lifecycle: airportd rolls back a refused sync ON (CONFIRMED)

Turning the Mac's Wi-Fi off and on while AirDrop is open produces this sequence (D1, 9 Nov):

```
22:27:28.843  Set AWDL_SYNC_ENABLED -> FAIL 0xe00002d8   (HAL stopping)
22:27:28.844  Set AWDL_SYNC_ENABLED -> GOOD              (airportd's rollback OFF, ~1 ms later)
```

The OFF correctly cancels D1's retry ticket (`SyncRecoveryArmed=2`, `Stale=2`, `Completed=0`). After that, airportd never sends the ON again. AWDL stays disabled until the next reassociation or reboot. **Returning an error is what kills the recovery.**

- **L2** accepts the ON, so airportd no longer sends the rollback. However, the ticket is armed against the HAL epoch that is stopping, and the restart always creates a new epoch. Without a reassociation, AWDL stays off: 2 of 3 cycles recovered, and the failed one was the unassociated case.
- **L3** follows the ticket into the new epoch. On hardware, 4 of 4 cycles recovered, 3 of them unassociated. AWDL came back within 3–9 s, and AirDrop worked afterwards at 0.74 MB/s.
  - Caveat: in those 4 cycles the HAL was already ready, so the re-arm path itself (`MTKAWDLSyncDeferred`, `MTKAWDLSyncRecoveryRearmed`) was **exercised only by the offline tests**. TO VERIFY.

### 2. Release 1.0.13 regressions on this machine (CONFIRMED)

These were seen with the stock 1.0.13 Wi-Fi kext (`b77a55b`) and unchanged boot-args.

- **No AWDL without an associated AP.** `MtkMwx::awdlEnable()` returns `kIOReturnNotReady` while `ic_state != IEEE80211_S_RUN` (hunk "hold AWDL off until the infra link is actually joined"). The ready gate waits 180 s, the bypass enable is refused (`SyncEnableStatus=0xe00002d8`), and macOS logs *"Did not get AW start event"*. On an Ethernet-only Mac, AirDrop never comes up.
- **No recovery after a Wi-Fi off/on cycle.** The ON arrives before RUN, is refused, and is followed by the rollback in §1. For more than 3 minutes after reassociation, with AirDrop open, there was no new request (`SyncRequests` stuck at 15). The code comment says *"airportd retries this call by itself"*. That holds at boot, where the first join triggers a new request, but not after this cycle.
- **With `amtk_awdl_peer_gate=0 amtk_awdl_renew_station=1`**, one boot never synced to the iPhone and unicast TX failed 92% of the time (24/26). `NoPeer` did not move, so `renew_station` never acted. This is a single case, so whether `peer_gate=0` is the cause is TO VERIFY.

### 3. Rate (CONFIRMED)

- With the 1.0.12 thresholds, the unicast ladder sat at 6 Mbps. With 16/32/64 it climbs 6 → 12 → 24 Mbps in about 40 ACKs and steps back down on errors (L4).
- TX-EFFICIENCY plus the 4 ms guard (L5) brings unicast TX errors from 4–5% to 2.5%, with the rate held at 65 Mbps (HT MCS7). Mac → iPhone went from 30–75 kB/s to 0.23 MB/s, and AirDrop now starts in about 2.5 s instead of up to 30 s.

### 4. Country code `"00"` and locale FCC put AWDL on channel 149 (CONFIRMED)

This is the largest single effect.

- **The bug.** `AirportMtkWlan::getCOUNTRY_CODE()` returns the firmware code (`sc_alpha2`, which is `"00"` on this card). It uses the airportd geolocation only when the firmware says `"ZZ"`. `AirportMtkSkywalkInterface::getLOCALE()` always returns `APPLE80211_LOCALE_FCC`. macOS therefore uses the US AWDL plan, with 149 as the data channel, even where 149 is not allowed for the AP (Italy, in this case).
- **Why it costs so much.** With the AP on 44, the channel sequence was `44, 149, 6, 149`. Every data window needed an off-channel ROC with a PM handshake (80–160 ms grant latency). `awdlDensePresenceAllowed()` also requires `infra == channel`, so dense presence never engaged.
- **With `amtk_cc=IT`.** The sequence becomes `44, 44, 6, 44` and dense presence engages under load (4–12 transitions). Unicast TX errors fall to 0.14–0.5%.

| AirDrop, 5.6–6 MB file | L5, country `00` | L5 + `amtk_cc=IT`, AP on 44 | L6 + `amtk_cc=IT` |
|---|---|---|---|
| Mac → iPhone | 0.13–0.23 MB/s | **≈0.9 MB/s** | 0.44 MB/s |
| iPhone → Mac | 0.33–0.70 MB/s | **≈5 MB/s** | 2.9 MB/s |

Throughput varies a lot from run to run. One or two runs per cell, so treat the numbers as orders of magnitude.

- **L6 code path (CONFIRMED).** L6 reports `MTKWiFiCountryCode=IT`, `MTKWiFiCountrySource=boot-arg` and `MTKWiFiLocale=2` (ETSI).
- **Geolocation fallback (TO VERIFY).** On this machine airportd never called `setCOUNTRY_CODE` (`MTKWiFiCountryGeo` stayed absent), so the fallback could not be exercised and the boot-arg is still needed here. The cause is unknown: Location Services, 802.11d from the AP, or the Skywalk path.

### 4b. Why the boot-arg was needed, and L7 (CONFIRMED)

- **airportd never sends the country.** It counts the country in the APs' Country elements (*"Currently highest country IT, count 7"*) and passes it to countryd, but never calls `setCOUNTRY_CODE`.
- **IO80211 logs *"Dynamic country code not supported on this device"*.** This depends on bit 7 of capability bytes 5 or 8 in `IO80211AWDLPeerManager::configure`, 25G332.
- **L7 reads the Country element in the driver**, as Broadcom firmware does (`EnabledAutoCountry`). Without `amtk_cc` it reports `MTKWiFiCountrySource = 802.11d-ap` (IT:12) and locale ETSI. The kernel logs `country code IT` / *"Disallowing Channel 149"*, and AWDL runs 44, 44, 6, 44.
- **AirDrop without the boot-arg** (11 Nov): iPhone → Mac 2.95 MB/s, Mac → iPhone 0.34 MB/s. That is the same as with `amtk_cc=IT`.

### 5. Still open

- **AWDL election (PROBABLE).** In every failed Mac → iPhone transfer, the Mac stayed AWDL master on its own (`HostRole=SyncRole=1`, `NativeParentSelected=No`). In successful transfers it was synced to the iPhone (`HostRole=2`). This is more frequent after a cold boot, when the BT firmware is downloaded again (23 s instead of 2.3 s), but cold boot is not the only factor.
- **TX aggregation.** The AWDL TXWI always sets `MT_TXD3_BA_DISABLE`. 1.0.13 adds A-MSDU. TX A-MPDU, with a BA originator, has not been tried.

## Tools

These scripts are read-only, need no sudo, and redact MAC, BSSID and SSID values.

- `tools/hackintosh/mtk-snapshot.py snap <label>` / `diff a.json b.json`: dumps the `AirportMtkWlan` and `AirportMtkAwdlInterface` ioreg counters, kext UUIDs and awdl0 netstat, and diffs two dumps.
- `tools/hackintosh/mtk-sampler.py <label> <seconds>` and `report <csv>`: a 1 s sampler for transfers. The report detects transfer bursts and gives throughput and TX error rate.

## Reproducing

```bash
bash scripts/bootstrap.sh
bash scripts/.build-tlv4-wifi.sh Release    # Wi-Fi kext only
bash scripts/test-awdl.sh                   # test-mi-discovery.py fails as on 1.0.12 (stale stub)
(cd rust && cargo test -p mtk-wlan)
c++ -std=c++17 -fsanitize=address,undefined tests/awdl/l6-country-locale-test.cpp -o /tmp/l6 && /tmp/l6
```
