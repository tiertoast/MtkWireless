// SPDX-License-Identifier: GPL-2.0-or-later
#include <AWDL/AwdlSyncRecovery.hpp>
#include <cassert>

using mtkawdl::SyncRecovery;
using mtkawdl::SyncRecoveryReadiness;

int main()
{
    using Action = SyncRecovery::Action;
    using Radio = SyncRecoveryReadiness;
    constexpr uint64_t now = 100;
    constexpr uint32_t epoch = 7;
    SyncRecovery request;
    assert(request.poll(now, Radio::Ready) == Action::Idle);
    const auto on = request.supersede();
    assert(request.arm(on, epoch, now));
    assert(request.epoch == epoch);
    assert(request.poll(now + 1, Radio::Waiting) == Action::Wait);
    assert(request.poll(now + 2, Radio::Ready) == Action::Retry);
    assert(request.poll(now + 2, Radio::Ready) == Action::Idle);

    const auto pending = request.supersede();
    assert(request.arm(pending, epoch, now));
    const auto off = request.supersede();
    assert(off != pending && !request.current(pending));
    assert(!request.arm(pending, epoch, now)); // ON completes late after OFF
    assert(request.poll(now, Radio::Ready) == Action::Idle);

    assert(request.arm(off, epoch, now));
    assert(request.poll(now, Radio::Stale) == Action::Stale); // another quiesce or detach
    assert(request.poll(now, Radio::Ready) == Action::Idle);

    assert(request.arm(request.supersede(), epoch, now));
    assert(request.poll(now + SyncRecovery::waitUS - 1, Radio::Waiting) == Action::Wait);
    assert(request.poll(now + SyncRecovery::waitUS, Radio::Ready) == Action::Expired);
    assert(request.poll(now + SyncRecovery::waitUS + 1, Radio::Ready) == Action::Idle);

    auto inflight = request.supersede();
    assert(request.arm(inflight, epoch, now));
    assert(request.poll(now, Radio::Ready) == Action::Retry);
    request.supersede(); // cancel while the MCU wait releases the native gate
    assert(!request.current(inflight));
}
