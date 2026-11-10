use mtk_wlan::awdl::protocol::*;
use mtk_wlan::awdl::timeline::*;

const COMPATIBLE_TAILS: [[u8; 2]; 4] = [[0, 0x24], [0x81, 0x24], [0, 0xdc], [0x20, 0xd4]];

fn captures() -> Vec<Vec<u8>> {
    let dir = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("tests/fixtures/mt7922-sync-20261106");
    let mut paths: Vec<_> = std::fs::read_dir(dir)
        .unwrap()
        .map(|e| e.unwrap().path())
        .filter(|p| p.extension().is_some_and(|s| s == "bin"))
        .collect();
    paths.sort();
    paths.iter().map(|p| std::fs::read(p).unwrap()).collect()
}

// Keep a real frame's fixed header and Sync Parameters fixed fields, replacing
// only the bounded TLV4 sequence. Other TLVs are intentionally omitted here.
fn action_with_sequence(seq: &[u8]) -> Vec<u8> {
    let f = &captures()[0];
    let mut out = f[..40].to_vec();
    out.push(4);
    out.extend_from_slice(&((33 + seq.len()) as u16).to_le_bytes());
    out.extend_from_slice(&f[43..76]);
    out.extend_from_slice(seq);
    out
}

fn sequence(encoding: u8) -> Vec<u8> {
    let mut s = vec![15, encoding, 0, 3, 255, 255];
    for _ in 0..16 {
        match encoding {
            0 => s.push(6),
            1 => s.extend_from_slice(&[0, 6]),
            3 => s.extend_from_slice(&[6, 81]),
            _ => unreachable!(),
        }
    }
    s
}

#[test]
fn captured_frames_are_accepted_without_changing_bytes() {
    let frames = captures();
    assert_eq!(frames.len(), 85);
    let mut accepted = 0;
    for (i, f) in frames.iter().enumerate() {
        assert!(valid_action_envelope(f), "frame {i} envelope");
        assert_eq!(&f[40..43], &[4, 73, 0]);
        assert!(matches!(&f[114..116], [0, 0x24] | [0x81, 0x24]));
        // All old validations pass when only these two bytes become zero.
        let mut normalized = f.clone();
        normalized[114..116].fill(0);
        assert!(parse_action(&normalized).is_some(), "normalized frame {i}");
        assert!(parse_sequence(&f[76..114]).is_some());
        assert!(parse_sequence(&f[76..116]).is_none()); // standalone stays strict
        let parsed = parse_action(f);
        if parsed.is_some() {
            accepted += 1;
            assert_eq!(parsed, parse_action(&normalized), "frame {i}");
        }
    }
    eprintln!(
        "captured replay: {accepted}/85 accepted; 85/85 accepted after zeroing only the two tail bytes"
    );
    assert_eq!(accepted, 85);
}

#[test]
fn ordinary_encodings_and_zero_padding_remain_supported() {
    for encoding in [0, 1, 3] {
        let seq = sequence(encoding);
        for padding in 0..=3 {
            let mut padded = seq.clone();
            padded.extend(std::iter::repeat_n(0, padding));
            assert!(parse_sequence(&padded).is_some());
            assert!(parse_action(&action_with_sequence(&padded)).is_some());
        }
    }
}

#[test]
fn compatibility_is_limited_to_exact_observed_embedded_shape() {
    for encoding in [0, 1, 3] {
        for tail in COMPATIBLE_TAILS {
            let mut seq = sequence(encoding);
            seq.extend_from_slice(&tail);
            assert_eq!(
                parse_action(&action_with_sequence(&seq)).is_some(),
                encoding == 1
            );
            assert!(parse_sequence(&seq).is_none());
        }
    }
    for tail in [[1, 0x24], [0x80, 0x24], [0, 0x25], [0x81, 0], [0xff, 0xff]] {
        let mut seq = sequence(1);
        seq.extend_from_slice(&tail);
        assert!(parse_action(&action_with_sequence(&seq)).is_none());
    }
    for count in [0u8, 7, 14] {
        let mut seq = sequence(1);
        seq[0] = count;
        seq.truncate(6 + (count as usize + 1) * 2);
        seq.extend_from_slice(&[0, 0x24]);
        assert!(parse_action(&action_with_sequence(&seq)).is_none());
    }
    let mut seq = sequence(1);
    seq[3] = 1; // the observed extension does not authorize other step counts
    seq.extend_from_slice(&[0, 0x24]);
    assert!(parse_action(&action_with_sequence(&seq)).is_none());
}

#[test]
fn only_zero_padding_and_four_captured_tails_are_accepted() {
    let mut frame = captures()[0].clone();
    for value in 0..=u16::MAX {
        let tail = value.to_le_bytes();
        frame[114..116].copy_from_slice(&tail);
        assert_eq!(
            parse_action(&frame).is_some(),
            tail == [0, 0] || COMPATIBLE_TAILS.contains(&tail),
            "tail {tail:02x?}"
        );
    }
}

#[test]
fn sequence_bounds_and_encodings_stay_strict() {
    for enc in [0, 1, 3] {
        let seq = sequence(enc);
        for n in 0..seq.len() {
            assert!(parse_action(&action_with_sequence(&seq[..n])).is_none());
        }
        for index in [0, 1, 2, 3, 4, 5] {
            let mut invalid = seq.clone();
            invalid[index] = 0xfe;
            assert!(parse_action(&action_with_sequence(&invalid)).is_none());
        }
        let mut invalid = seq.clone();
        invalid[6 + usize::from(enc == 1)] = 166;
        assert!(parse_action(&action_with_sequence(&invalid)).is_none());
        let mut over = seq;
        over.extend_from_slice(&[0; 4]);
        assert!(parse_action(&action_with_sequence(&over)).is_none());
    }
    let mut invalid_opclass = sequence(3);
    invalid_opclass[7] = 128;
    assert!(parse_action(&action_with_sequence(&invalid_opclass)).is_none());
    for extension in [vec![0x24], vec![0, 0x24, 0], vec![0, 0x24, 0, 0]] {
        let mut seq = sequence(1);
        seq.extend_from_slice(&extension);
        assert!(parse_action(&action_with_sequence(&seq)).is_none());
    }
}

#[test]
fn tlv_framing_and_sync_checks_remain_required() {
    let mut seq = sequence(1);
    seq.extend_from_slice(&[0, 0x24]);
    let f = action_with_sequence(&seq);
    for len in [0u16, 32, 38, 72, 74, u16::MAX] {
        let mut g = f.clone();
        g[41..43].copy_from_slice(&len.to_le_bytes());
        assert!(parse_action(&g).is_none(), "length {len}");
    }
    for offset in [43 + 5, 43 + 27, 43 + 29] {
        let mut g = f.clone();
        g[offset] ^= 1;
        assert!(parse_action(&g).is_none());
    }
    let mut duplicate = f.clone();
    duplicate.extend_from_slice(&f[40..]);
    assert!(parse_action(&duplicate).is_none());
    // Standalone TLV18 cannot inherit TLV4's compatibility policy.
    let mut extra = f;
    extra.push(18);
    extra.extend_from_slice(&(seq.len() as u16).to_le_bytes());
    extra.extend_from_slice(&seq);
    assert!(parse_action(&extra).is_none());
}

#[test]
fn malformed_combinations_do_not_panic_or_create_a_parent() {
    let f = &captures()[0];
    for n in 0..f.len() {
        let mut state = State::default();
        state.init(&[2, 1, 2, 3, 4, 5], 1_000_000_000);
        let _ = parse_action(&f[..n]);
        let _ = state.receive_action(&f[..n], 1_000_000_000);
        assert!(!state.native_parent_selected);
    }
    let mut seed = 0xa591_7212u32;
    for _ in 0..10_000 {
        let mut g = f.clone();
        for _ in 0..4 {
            seed ^= seed << 13;
            seed ^= seed >> 17;
            seed ^= seed << 5;
            let index = seed as usize % g.len();
            g[index] ^= (seed >> 24) as u8;
        }
        let _ = parse_action(&g);
        let mut state = State::default();
        state.init(&[2, 1, 2, 3, 4, 5], 1_000_000_000);
        let _ = state.receive_action(&g, 1_000_000_000);
        assert!(!state.native_parent_selected);
    }
}

#[test]
fn real_peer_requires_native_selection_then_can_schedule_and_expire() {
    let f = &captures()[0];
    let a = parse_action(f).expect("real capture");
    let peer: [u8; 6] = f[10..16].try_into().unwrap();
    let now = 1_000_000_000;
    let mut state = State::default();
    state.init(&[2, 1, 2, 3, 4, 5], now);
    state.native_election_managed = true;
    assert!(state.set_sync_role(2, Some(&peer)));
    assert!(!state.radio_schedule_ready());
    assert!(state.receive_action(f, now) >= 0);
    assert!(!state.native_parent_selected);
    assert!(!state.radio_schedule_ready()); // accepting RX alone cannot authorize sync
    state.select_native_parent(&peer, Some(&a.master), true);
    state.elect(now);
    assert!(state.native_parent_selected && state.native_clock_validated);
    assert!(state.synchronized && state.radio_schedule_ready());
    assert!(plan_radio(&state, now, 55_000, false, None, 0, 0).is_some());
    let mut members = DataPeers::default();
    assert!(members.observe(&peer, now) >= 0);
    assert!(members.find(&peer, now) >= 0);
    assert!(members.find(&peer, now + PEER_LIFETIME_US + 1) < 0);
}

#[test]
fn accepted_embedded_shape_can_be_refreshed_without_emitting_unknown_tail() {
    let mut state = State::default();
    let now = 1_000_000_000;
    state.init(&[2, 1, 2, 3, 4, 5], now);
    for original in captures() {
        let mut frame = original.clone();
        let n = refresh_action(&mut state, &mut frame, now);
        assert!(n > 0 && n <= original.len());
        let parsed = parse_action(&frame[..n]).expect("refreshed template");
        assert_eq!(parsed.sequence, state.sequence);
        // The writer emits its existing canonical encoding-0 format.
        assert_eq!(frame[77], 0);
        let len = u16::from_le_bytes([frame[41], frame[42]]) as usize;
        assert!(parse_sequence(&frame[76..43 + len]).is_some());
    }
}

#[test]
fn stock_frames_replay_with_three_independent_timing_rejections() {
    let dir = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("tests/fixtures/mt7922-stock-20261108");
    let mut paths: Vec<_> = std::fs::read_dir(dir)
        .unwrap()
        .map(|entry| entry.unwrap().path())
        .filter(|path| path.file_name().unwrap().to_str().unwrap().starts_with("frame-"))
        .collect();
    paths.sort();
    assert_eq!(paths.len(), 19);
    let mut accepted = 0;
    for path in paths {
        let name = path.file_name().unwrap().to_str().unwrap();
        let frame = std::fs::read(&path).unwrap();
        let tail = frame[114..116].try_into().unwrap();
        assert!(COMPATIBLE_TAILS.contains(&tail), "{name}");
        assert!(parse_sequence(&frame[76..116]).is_none(), "{name}");
        let expected = !matches!(name, "frame-18.bin" | "frame-19.bin" | "frame-26.bin");
        let mut zeroed = frame.clone();
        zeroed[114..116].fill(0);
        let parsed = parse_action(&frame);
        assert_eq!(parsed.is_some(), expected, "{name}");
        assert_eq!(parsed, parse_action(&zeroed), "{name}");

        let now = 1_000_000_000;
        let mut state = State::default();
        state.init(&[2, 1, 2, 3, 4, 5], now);
        assert_eq!(state.receive_action(&frame, now) >= 0, expected, "{name}");
        if expected {
            accepted += 1;
            let mut refreshed = frame;
            let length = refresh_action(&mut state, &mut refreshed, now);
            assert!(length > 0, "{name}");
            let sync_len = u16::from_le_bytes([refreshed[41], refreshed[42]]) as usize;
            assert_eq!(refreshed[77], 0, "{name}");
            assert!(parse_sequence(&refreshed[76..43 + sync_len]).is_some(), "{name}");
            assert!(parse_action(&refreshed[..length]).is_some(), "{name}");
        }
    }
    assert_eq!(accepted, 16);
    eprintln!("stock replay: {accepted}/19 accepted, samples 18/19/26 rejected");
}

#[test]
fn each_compatible_tail_preserves_channel_election_and_refresh_guards() {
    let original = &captures()[0];
    let mut tlv = 40;
    while original[tlv] != 24 {
        tlv += 3 + u16::from_le_bytes([original[tlv + 1], original[tlv + 2]]) as usize;
    }
    for tail in COMPATIBLE_TAILS {
        let mut frame = original.clone();
        frame[114..116].copy_from_slice(&tail);
        let mut bad_election = frame.clone();
        bad_election[tlv + 3] |= 1;
        assert!(parse_action(&bad_election).is_none());

        let mut bad_channel = frame.clone();
        bad_channel[83] = 166;
        assert!(parse_action(&bad_channel).is_none());
        let mut standalone = frame[76..116].to_vec();
        let mut extra = action_with_sequence(&standalone);
        extra.extend_from_slice(&[18, 40, 0]);
        extra.append(&mut standalone);
        assert!(parse_action(&extra).is_none());

        let mut state = State::default();
        let now = 1_000_000_000;
        state.init(&[2, 1, 2, 3, 4, 5], now);
        assert_eq!(refresh_action(&mut state, &mut bad_channel, now), 0);
        assert_eq!(refresh_action(&mut state, &mut extra, now), 0);
        frame[114..116].copy_from_slice(&[0xff, 0xfe]);
        assert_eq!(refresh_action(&mut state, &mut frame, now), 0);
    }
}
