// SPDX-License-Identifier: GPL-2.0-or-later
#pragma once

#include <stdint.h>

namespace mtkawdl {

struct TxGrant {
    uint64_t generation=0, observed=0, deadline=0;
    uint32_t epoch=0;
    uint8_t token=0, channel=0;

    bool matches(const TxGrant &live) const {
        return generation==live.generation && epoch==live.epoch && token==live.token &&
            channel==live.channel && observed==live.observed && deadline==live.deadline;
    }
};

struct TxRendezvous {
    TxGrant grant;
    uint64_t end=0, expiry=0;

    bool valid(const TxGrant &live,bool usable,uint64_t now) const {
        return usable && grant.matches(live) && now>=grant.observed &&
            now<grant.deadline && now<end && now<expiry;
    }
};

// Keep a deferred prefix in place while allowing other peers and controls to
// pass it. The offset is bounded by the queue length at the start of the pass.
template<class Move>
unsigned removeTxCandidate(unsigned head,unsigned offset,unsigned size,Move move) {
    for(unsigned i=offset;i;--i) move((head+i)%size,(head+i-1)%size);
    return (head+1)%size;
}

template<class Move>
unsigned restoreTxCandidate(unsigned head,unsigned offset,unsigned size,Move move) {
    head=(head+size-1)%size;
    for(unsigned i=0;i<offset;++i) move((head+i)%size,(head+i+1)%size);
    return head;
}

} // namespace mtkawdl
