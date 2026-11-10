"""Run the production native queue drain and DMA commit with a synthetic clock."""
from pathlib import Path
import subprocess
import tempfile

root = Path(__file__).resolve().parents[2]
native = (root / "AirportMtkWlan/AirportMtkAwdlInterface.cpp").read_text()
hal = (root / "SimpleMtkWlan/hal_mwx/MtkMwx.cpp").read_text()
start = native.index("    unsigned pending=queueCount, parked=0;")
drain = native[start:native.index("    if(!hal->awdlCanTransmit(channel))", start)]
start = hal.index("static int\nmwx_dma_txwi_enqueue_locked(")
commit = hal[start:hal.index("\n}\n", start) + 3]

code = r'''
#include <cassert>
#include <cerrno>
#include <cstdio>
#include <cstring>
#include <initializer_list>
#include <vector>
#include <AWDL/AwdlTimeline.hpp>
#include <AWDL/AwdlTxRendezvous.hpp>

using IOReturn=int;
constexpr auto kIOReturnSuccess=0,kIOReturnNotFound=1,kIOReturnNotReady=2,kIOReturnAborted=3;
uint64_t clockUS=0;
uint64_t awdlNow(){return clockUS;}
unsigned testGuardUS=0;
unsigned awdlTxGuardUS(){return testGuardUS;}
struct Frame { uint8_t bytes[64]{}; unsigned id; };
using mbuf_t=Frame *;
void *mbuf_data(mbuf_t m){return m->bytes;}
size_t mbuf_pkthdr_len(mbuf_t){return sizeof(Frame::bytes);}
void mbuf_freem(mbuf_t m){delete m;}
struct MockHal {
    mtkawdl::TxGrant grant;
    std::vector<unsigned> submitted;
    uint64_t preparationUS=0;
    unsigned rejectID=0;
    uint64_t lastEnd=0;
    bool awdlCanTransmit(uint8_t){return true;}
    bool awdlTxGrantSnapshot(uint8_t,mtkawdl::TxGrant &out){out=grant;return true;}
    IOReturn awdlTryTransmit(mbuf_t *packet,uint8_t,const mtkawdl::TxRendezvous *limit){
        auto m=*packet;
        if(m->id==rejectID) {
            if(limit && !limit->valid(grant,true,clockUS+preparationUS)) return kIOReturnAborted;
            return kIOReturnNotReady;
        }
        assert(!limit || limit->valid(grant,true,clockUS));
        if(limit) lastEnd=limit->end;
        submitted.push_back(m->id); delete m; *packet=nullptr;
        return kIOReturnSuccess;
    }
};
struct TestInterface {
    static constexpr unsigned kDataQueueSize=8;
    Frame *queue[kDataQueueSize]{};
    uint64_t queueTime[kDataQueueSize]{},queueDeadline[kDataQueueSize]{};
    unsigned queueHead=7,queueCount=0;
    MockHal radio,*hal=&radio;
    mtkawdl::State state{};
    mtkawdl::Timeline awTimeline{};
    uint64_t addbaExpired=0,umiExpired=0,dataTxExpired=0;
    uint64_t addbaDeferred=0,umiDeferred=0,dataTxDeferred=0;
    uint64_t addbaSubmitted=0,umiSubmitted=0,dataTxSubmitted=0;
    uint64_t addbaFailed=0,umiFailed=0,dataTxFailed=0;
    uint64_t txRendezvousDeferred=0,txRendezvousAvoided=0,txRendezvousExpired=0,txRendezvousGuarded=0;
    IOReturn umiStatus=0,dataTxStatus=0;
    mbuf_t makeFrame(const uint8_t *,size_t){assert(false);return nullptr;}
    void push(unsigned id,uint64_t expiry,const uint8_t *peer,bool action=false) {
        auto *m=new Frame{};
        memcpy(m->bytes+4,peer,6);
        m->id=id; m->bytes[0]=action ? 0xd0 : 0x08;
        if(action) m->bytes[24]=mtkawdl::blockAckCategory;
        unsigned slot=(queueHead+queueCount++)%kDataQueueSize;
        queue[slot]=m; queueTime[slot]=123; queueDeadline[slot]=expiry;
    }
    void queued(std::initializer_list<unsigned> ids,uint64_t expiry){
        assert(queueCount==ids.size()); unsigned slot=queueHead;
        for(auto id:ids){
            assert(queue[slot]->id==id && queueTime[slot]==123 && queueDeadline[slot]==expiry);
            slot=(slot+1)%kDataQueueSize;
        }
    }
    ~TestInterface(){
        for(unsigned i=0;i<queueCount;++i) delete queue[(queueHead+i)%kDataQueueSize];
    }
    void run(uint64_t now,const mtkawdl::Reservation &grant) {
        constexpr uint8_t channel=149;
''' + drain + r'''
    }
};

#define BUS_DMASYNC_PREWRITE 1
#define BUS_DMASYNC_POSTWRITE 2
#define MT_DMA_CPU_IDX 8
#define MT_DMA_CTL_LAST_SEC0 1
#define MT_DMA_CTL_SD_LEN0(n) (n)
#define KASSERT(x) assert(x)
uint32_t htole32(uint32_t value){return value;}
#define MWX_DEV_LOG(...) ((void)0)
struct Map { uint32_t dm_mapsize; };
struct mwx_txwi { uint32_t *mt_desc; uint32_t mt_addr; };
struct mwx_queue_data { mwx_txwi *md_txwi; void *md_mbuf; Map *md_map; };
struct mwx_desc { uint32_t buf0,buf1,info,ctrl; };
struct mwx_queue {
    int mq_prod,mq_cons,mq_count;
    mwx_queue_data *mq_data;
    mwx_desc *mq_desc;
    uint32_t mq_regbase;
    Map *mq_map;
};
struct mwx_softc {
    void *sc_dmat=nullptr;
    bool locked=false,*sc_awdl_lock=&locked;
    uint64_t sc_awdl_tx_rendezvous_rejected=0;
    mtkawdl::TxGrant live;
    bool usable=true, requireLock=true;
    int doorbells=0;
};
void IOLockLock(bool *lock){assert(!*lock);*lock=true;}
void IOLockUnlock(bool *lock){assert(*lock);*lock=false;}
void bus_dmamap_sync(void*,Map*,unsigned,unsigned,unsigned){}
void bus_dmamap_unload(void*,Map*){}
unsigned mwx_dma_free_slots(mwx_queue*){return 3;}
void mwx_write(mwx_softc *sc,unsigned,unsigned){assert(sc->locked==sc->requireLock);++sc->doorbells;}
uint64_t mwx_awdl_now(){return clockUS;}
mtkawdl::TxGrant mwx_awdl_tx_grant(mwx_softc *sc){assert(sc->locked);return sc->live;}
bool mwx_awdl_tx_usable(mwx_softc *sc,uint8_t,uint64_t){assert(sc->locked);return sc->usable;}
''' + commit + r'''
int main(){
    const uint8_t local[]={2,1,2,3,4,5},a[]={2,1,2,3,4,6},b[]={2,1,2,3,4,7};
    const uint8_t group[]={0x33,0x33,0,0,0,1};
    constexpr uint64_t start=1000000,aw=mtkawdl::awUs,end=start+16*aw;
    TestInterface test;
    test.state.init(local,start);
    auto &sequence=test.state.sequence;
    sequence.count=4; sequence.step=3;
    sequence.channels[0]=sequence.channels[1]=149;
    sequence.channels[2]=6; sequence.channels[3]=149;
    for(unsigned i=0;i<2;++i){
        auto &p=test.state.peers[i];
        memcpy(p.mac,i?a:b,6); p.used=true; p.seen=p.epoch=start;
        p.advertisement.sequence=sequence;
    }
    test.state.peers[1].advertisement.sequence.channels[1]=6;
    mtkawdl::Reservation grant{};
    grant.request(7,149,start); assert(grant.grant(7,149,300,start));
    test.radio.grant={19,start,grant.deadline,2,7,149};
    test.push(1,end,a); test.push(2,end,b); test.push(3,end,a); test.push(4,end,b,true);
    test.push(5,end,group);
    // The first peer changed channel since the batch timestamp. B and the
    // BA action still pass; multicast keeps its original batch-time test.
    clockUS=start+4*aw;
    test.run(start+aw,grant);
    assert((test.radio.submitted==std::vector<unsigned>{2,4,5}));
    test.queued({1,3},end);
    assert(test.txRendezvousAvoided==1 && test.txRendezvousDeferred==1);
    // Crossing the contiguous local EAW boundary was valid for B. A can
    // now send in a later common slot, in original order.
    clockUS=start+12*aw;
    test.radio.rejectID=1; test.radio.preparationUS=4*aw;
    test.run(clockUS,grant);
    test.queued({1,3},end);
    assert(test.txRendezvousAvoided==2 && test.txRendezvousDeferred==2);
    test.radio.rejectID=0;
    test.run(clockUS,grant);
    assert((test.radio.submitted==std::vector<unsigned>{2,4,5,1,3}));
    test.push(6,clockUS,a); test.run(clockUS,grant);
    assert(test.txRendezvousExpired==1 && !test.queueCount);
    // T2: end-of-window guard. A frame is refused when the common window
    // has no more than the guard left, and otherwise its DMA commit bound
    // is the shortened end (the HAL re-checks it under its lock).
    clockUS=start+12*aw;
    {
        uint64_t common=mtk_awdl_data_rendezvous_end(&test.state,&test.state.peers[1],
            clockUS,test.radio.grant.deadline,149);
        assert(common>clockUS+2000);
        testGuardUS=unsigned(common-clockUS);
        test.push(7,end,a); test.run(clockUS,grant);
        test.queued({7},end);
        assert(test.txRendezvousGuarded==1 && test.radio.submitted.back()==3);
        testGuardUS=unsigned(common-clockUS)-1000;
        test.run(clockUS,grant);
        assert(test.radio.submitted.back()==7 && !test.queueCount);
        assert(test.radio.lastEnd==common-testGuardUS);
        // Guard 0 is exactly the previous D1 TX-EFFICIENCY behaviour.
        testGuardUS=0; test.push(8,end,a); test.run(clockUS,grant);
        assert(test.radio.submitted.back()==8 && test.radio.lastEnd==common);
    }
    puts("PASS T2 guard: refused within the guard, shortened commit bound otherwise, guard 0 unchanged.");

    mwx_softc sc; sc.live=test.radio.grant;
    mwx_queue_data data[4]{};mwx_desc desc[4]{};Map map{};
    mwx_queue q{0,3,4,data,desc,0,&map};
    mwx_txwi wi{nullptr,0x200};
    mtkawdl::TxRendezvous valid{sc.live,clockUS+10,clockUS+20};
    assert(!mwx_dma_txwi_enqueue_locked(&sc,&q,&wi,&valid));
    assert(sc.doorbells==1 && q.mq_prod==1 && data[0].md_txwi==&wi && !sc.locked);
    for(unsigned cause=0;cause<7;++cause){
        sc.live=valid.grant; sc.usable=true;
        auto attempt=valid;
        switch(cause){
        case 0: ++sc.live.generation; break;
        case 1: ++sc.live.epoch; break;
        case 2: ++sc.live.token; break;
        case 3: ++sc.live.observed; break;
        case 4: attempt.end=clockUS; break;
        case 5: attempt.expiry=clockUS; break;
        case 6: sc.usable=false; break;
        }
        assert(mwx_dma_txwi_enqueue_locked(&sc,&q,&wi,&attempt)==ESTALE);
        assert(!sc.locked && q.mq_prod==1 && sc.doorbells==1 && !data[1].md_txwi);
    }
    assert(sc.sc_awdl_tx_rendezvous_rejected==7);
    sc.requireLock=false;
    assert(!mwx_dma_txwi_enqueue_locked(&sc,&q,&wi,nullptr));
    assert(q.mq_prod==2 && sc.doorbells==2);
    puts("PASS: production drain and DMA commit, fresh peer edge, contiguous channel, expiry, mixed queue order and ownership, grant identity, unchanged unguarded path.");
}
'''

with tempfile.TemporaryDirectory(prefix="mtk-d1-tx-") as directory:
    source = Path(directory) / "test.cpp"
    binary = source.with_suffix("")
    source.write_text(code)
    subprocess.run(["clang++", "-std=c++14", "-Wall", "-Wextra", "-Werror",
                    "-fsanitize=address,undefined", "-I" + str(root / "include"),
                    str(source), "-o", str(binary)], check=True)
    subprocess.run([str(binary)], check=True, timeout=15)
