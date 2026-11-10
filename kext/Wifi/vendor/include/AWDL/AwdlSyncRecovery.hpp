// SPDX-License-Identifier: GPL-2.0-or-later
#pragma once

#include <stdint.h>

namespace mtkawdl {

enum class SyncRecoveryReadiness { Stale, Waiting, Ready };
struct SyncStop { uint32_t epoch=0; bool blocked=false; };

// One ticket for an ON rejected at HAL quiesce. The native gate owns all
// fields. The ticket is consumed before reentering the firmware enable path.
struct SyncRecovery {
    enum class Action { Idle, Wait, Retry, Stale, Expired };
    static constexpr uint64_t waitUS = 15000000;
    uint64_t generation = 0, deadline = 0;
    uint32_t epoch = 0;

    bool pending() const { return deadline != 0; }
    bool current(uint64_t request) const { return request == generation; }
    uint64_t supersede() { deadline = 0; return ++generation; }

    bool arm(uint64_t request, uint32_t stoppedEpoch, uint64_t now)
    {
        if(!current(request)) return false;
        epoch=stoppedEpoch;
        deadline=now+waitUS;
        return true;
    }

    // LIFECYCLE v3: follow the same native request into a newer HAL epoch.
    // The absolute deadline of the first acceptance is kept, so re-arming
    // never extends the bounded wait.
    bool rearm(uint64_t request, uint32_t newEpoch, uint64_t absoluteDeadline, uint64_t now)
    {
        if(!current(request) || now>=absoluteDeadline) return false;
        epoch=newEpoch;
        deadline=absoluteDeadline;
        return true;
    }

    Action poll(uint64_t now, SyncRecoveryReadiness radio)
    {
        if(!pending()) return Action::Idle;
        if(radio!=SyncRecoveryReadiness::Stale && now<deadline) {
            if(radio==SyncRecoveryReadiness::Waiting) return Action::Wait;
            deadline=0;
            return Action::Retry;
        }
        Action action=radio==SyncRecoveryReadiness::Stale ? Action::Stale : Action::Expired;
        deadline=0;
        return action;
    }
};

} // namespace mtkawdl
