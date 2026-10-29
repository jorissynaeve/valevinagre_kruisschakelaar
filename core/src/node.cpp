#include "kk/node.h"

#include <string.h>

#include "kk/cfghash.h"

namespace kk {

static inline bool due(uint32_t now, uint32_t at) { return int32_t(now - at) >= 0; }

Node::Node(Platform& p, const Key& key, Config& cfg, const Tuning& t) : p_(p), key_(key), cfg_(cfg), t_(t) {}

// ---------------------------------------------------------------- start

void Node::begin() {
  uint32_t now = p_.nowMs();
  cfg_.boot++;
  ctr_ = 0;
  p_.save(cfg_);
  // Relais eerst: het licht moet in de laatste stand staan voor er een radio of wifi is.
  for (uint8_t i = 0; i < cfg_.nrelays; i++) p_.setRelay(i, cfg_.relay[i].on);
  for (uint8_t i = 0; i < cfg_.nind; i++) {
    FollowCfg& f = cfg_.follow[i];
    if (f.valid && f.node == cfg_.id && f.relay < cfg_.nrelays) {
      ind_[i] = {true, bool(cfg_.relay[f.relay].on), cfg_.relay[f.relay].ver};
      p_.setIndicator(i, ind_[i].on);
    } else {
      p_.setIndicator(i, false);
    }
  }
  lastHeard_ = now;
  lastBeacon_ = now;
  bootAt_ = now;
  beaconLostCur_ = t_.beaconLostMs;
  nextHb_ = now + rnd(500, 5000);
  nextBeacon_ = now + rnd(0, 2000);
  if (radioCapable()) p_.setChannel(cfg_.channel);
}

// ---------------------------------------------------------------- uitzenden

Frame Node::mk(Type t) {
  if (ctr_ == 0xFFFF) {  // nonce-ruimte van deze boot op: nieuwe boot-waarde
    cfg_.boot++;
    ctr_ = 0;
    p_.save(cfg_);
  }
  Frame f;
  f.type = t;
  f.ttl = DEFAULT_TTL;
  f.id = {cfg_.id, cfg_.boot, ctr_++};
  f.len = 0;
  return f;
}

void Node::sendUplinkCopy(const uint8_t* d, size_t n) {
  uint8_t c[MAX_FRAME];
  memcpy(c, d, n);
  c[TTL_OFFSET] = DEFAULT_TTL;  // de LAN is een nieuw hop-domein
  for (uint8_t i = 0; i < t_.uplinkCopies; i++) p_.uplinkSend(c, n);
}

void Node::emit(Frame& f, uint8_t copies, uint16_t gapMin, uint16_t gapMax, uint32_t delay) {
  uint8_t buf[MAX_FRAME];
  size_t n = seal(key_, f, buf);
  if (!n) return;
  replay_.accept(f.id.src, f.id.key(), p_.nowMs());  // eigen echo later negeren
  if ((cfg_.flags & (CF_BRIDGE | CF_UPLINK_ONLY)) && !isLinkLocal(f.type)) sendUplinkCopy(buf, n);
  if (!radioCapable()) return;
  for (auto& o : q_) {
    if (o.used) continue;
    memcpy(o.buf, buf, n);
    o.len = uint8_t(n);
    o.used = true;
    o.fwd = false;
    o.dups = 0;
    o.rep = copies ? copies - 1 : 0;
    o.gapMin = gapMin;
    o.gapMax = gapMax;
    o.at = p_.nowMs() + delay;
    return;
  }
  stats.qDrops++;
}

void Node::queueForward(const uint8_t* d, size_t n, uint8_t ttl) {
  for (auto& o : q_) {
    if (o.used) continue;
    memcpy(o.buf, d, n);
    o.buf[TTL_OFFSET] = ttl;
    o.len = uint8_t(n);
    o.used = true;
    o.fwd = true;
    o.dups = 0;
    o.supp = (d[1] == uint8_t(Type::Heartbeat) || d[1] == uint8_t(Type::Beacon)) ? t_.fwdSuppressBulk : t_.fwdSuppress;
    o.rep = 0;
    o.at = p_.nowMs() + rnd(t_.fwdJitterMin, t_.fwdJitterMax);
    return;
  }
  stats.qDrops++;
}

bool Node::dupOfPending(const uint8_t* d, size_t n) {
  for (auto& o : q_)
    if (o.used && o.fwd && o.len == n && memcmp(o.buf + TTL_OFFSET + 1, d + TTL_OFFSET + 1, n - TTL_OFFSET - 1) == 0) {
      o.dups++;
      return true;
    }
  return false;
}

void Node::dropForwards() {
  for (auto& o : q_)
    if (o.used && o.fwd) o.used = false;
}

void Node::flush() {
  uint32_t now = p_.nowMs();
  for (auto& o : q_) {
    if (!o.used || !due(now, o.at)) continue;
    if (o.fwd && o.dups >= o.supp) {
      o.used = false;
      stats.suppressed++;
      continue;
    }
    // Kopie: radioSend mag (in de simulator) nooit re-entrant in de queue schrijven, maar zo blijft het veilig.
    uint8_t tmp[MAX_FRAME];
    uint8_t len = o.len;
    memcpy(tmp, o.buf, len);
    bool fwd = o.fwd;
    if (o.rep > 0) {
      o.rep--;
      o.at = now + rnd(o.gapMin, o.gapMax);
    } else {
      o.used = false;
    }
    p_.radioSend(tmp, len);
    stats.tx++;
    if (fwd) stats.txFwd++;
  }
}

// ---------------------------------------------------------------- ontvangen

void Node::onRadio(const uint8_t* d, size_t n) {
  if (!radioCapable()) return;
  if (dupOfPending(d, n)) return;
  accept(d, n, false);
}

void Node::onUplink(const uint8_t* d, size_t n) { accept(d, n, true); }

void Node::accept(const uint8_t* d, size_t n, bool fromUplink) {
  Frame f;
  if (!open(key_, d, n, f)) {
    stats.badAuth++;
    return;
  }
  if (f.id.src == cfg_.id) return;
  if (fromUplink && isLinkLocal(f.type)) return;
  uint32_t now = p_.nowMs();
  if (!replay_.accept(f.id.src, f.id.key(), now)) {
    stats.dups++;
    return;
  }
  stats.rx++;
  if (!fromUplink) lastHeard_ = now;
  handle(f);
  p_.onFrame(f, fromUplink);

  if (!fromUplink && isBridge() && !isLinkLocal(f.type)) sendUplinkCopy(d, n);
  if (isOneHop(f.type) || !radioCapable() || scan_.active || sw_.active) return;
  if (fromUplink)
    queueForward(d, n, f.ttl);
  else if (f.ttl > 1)
    queueForward(d, n, uint8_t(f.ttl - 1));
}

void Node::handle(const Frame& f) {
  Reader r(f.payload, f.len);
  switch (f.type) {
    case Type::BtnEvent: {
      uint8_t b = r.u8(), a = r.u8(), evt = r.u8();
      if (r.bad || a != 0) break;
      Cause c{f.id.src, b, evt};
      if (!evtDuplicate(f.id.src, f.id.boot, b, evt))
        trigger(f.id.src, b, c);
      else
        confirmDuplicate(f.id.src, b, c);  // een herhaling: de afzender heeft onze bevestiging blijkbaar gemist
      break;
    }
    case Type::LightState: {
      uint8_t relay = r.u8(), on = r.u8();
      uint16_t ver = r.u16();
      NodeId cs = r.u16();
      uint8_t cb = r.u8(), ce = r.u8();
      if (r.bad) break;
      noteState(f.id.src, relay, on != 0, ver);
      if (cs == cfg_.id && cb < 8 && ack_[cb].active && ack_[cb].evt == ce) ack_[cb].active = false;  // bevestiging van ons event
      break;
    }
    case Type::Heartbeat: {
      r.u16();  // fw
      r.u8();   // flags
      uint8_t nrel = r.u8();
      for (uint8_t i = 0; i < nrel && !r.bad; i++) {
        uint8_t on = r.u8();
        uint16_t ver = r.u16();
        if (!r.bad) noteState(f.id.src, i, on != 0, ver);
      }
      break;
    }
    case Type::Probe:
      if (radioCapable() && !scan_.active && !sw_.active) emitChannelInfo(Type::ProbeResp, rnd(0, 8));
      break;
    case Type::Beacon:
      lastBeacon_ = p_.nowMs();
      sawBeacon_ = true;
      bootScanDone_ = true;
      beaconLostCur_ = t_.beaconLostMs;
      scanFails_ = 0;
      // fallthrough
    case Type::ProbeResp:
    case Type::Announce: {
      uint8_t chan = r.u8();
      uint16_t epoch = r.u16();
      uint8_t flags = r.u8();
      if (!r.bad) noteChannel(chan, epoch, flags);
      break;
    }
    case Type::CmdLight: {
      NodeId dst = r.u16();
      uint8_t relay = r.u8(), op = r.u8();
      if (!r.bad && dst == cfg_.id && relay < cfg_.nrelays && op <= 2) applyOp(relay, Mode(op), Cause{f.id.src, 0xFF, uint8_t(f.id.ctr)}, true);
      break;
    }
    case Type::CfgSet:
      applyCfg(f);
      break;
    case Type::Learn: {
      uint8_t on = r.u8();
      if (r.bad) break;
      learnOn_ = on != 0;
      learnUntil_ = p_.nowMs() + t_.learnMs;
      break;
    }
    case Type::Identify: {
      NodeId dst = r.u16();
      if (!r.bad && (dst == cfg_.id || dst == ID_BROADCAST)) p_.identify();
      break;
    }
    default:
      break;
  }
}

// ---------------------------------------------------------------- lichten

bool Node::learning() const { return learnOn_ && int32_t(learnUntil_ - p_.nowMs()) > 0; }

void Node::trigger(NodeId src, uint8_t btn, const Cause& cause) {
  if (learning()) return;  // tijdens het leren schakelt niets
  for (uint8_t i = 0; i < cfg_.nrelays; i++) {
    const RelayCfg& r = cfg_.relay[i];
    for (uint8_t k = 0; k < r.ntrig; k++) {
      if (r.trig[k].node == src && r.trig[k].btn == btn) {
        applyOp(i, Mode(r.trig[k].mode), cause, false);
        break;
      }
    }
  }
}

bool Node::applyOp(uint8_t relay, Mode m, const Cause& cause, bool forceReport) {
  RelayCfg& r = cfg_.relay[relay];
  bool cur = r.on != 0;
  bool on = m == Mode::Toggle ? !cur : m == Mode::On;
  bool changed = on != cur;
  if (changed) {
    r.on = on;
    r.ver++;
    p_.setRelay(relay, on);
    p_.save(cfg_);
    stats.lightChanges++;
    noteState(cfg_.id, relay, on, r.ver);
    hbFast_ = t_.hbFollowUps;  // snel een paar heartbeats: wie de LIGHT_STATE miste, krijgt een tweede kans
    uint32_t soon = p_.nowMs() + rnd(t_.hbFastMin / 2, t_.hbFastMin);
    if (int32_t(nextHb_ - soon) > 0) nextHb_ = soon;
  }
  if (changed || forceReport) emitLightState(relay, cause);
  return changed;
}

void Node::emitLightState(uint8_t relay, const Cause& cause) {
  Frame f = mk(Type::LightState);
  Writer w(f.payload);
  w.u8(relay);
  w.u8(cfg_.relay[relay].on);
  w.u16(cfg_.relay[relay].ver);
  w.u16(cause.src);
  w.u8(cause.btn);
  w.u8(cause.evt);
  f.len = uint8_t(w.n);
  emit(f, 2, 12, 25, 0);
}

void Node::noteState(NodeId node, uint8_t relay, bool on, uint16_t ver) {
  for (uint8_t i = 0; i < cfg_.nind; i++) {
    const FollowCfg& fc = cfg_.follow[i];
    if (!fc.valid || fc.node != node || fc.relay != relay) continue;
    Ind& in = ind_[i];
    if (in.known && !newer16(ver, in.ver)) continue;
    bool changed = !in.known || in.on != on;
    in = {true, on, ver};
    if (changed) p_.setIndicator(i, on);
  }
}

void Node::confirmDuplicate(NodeId src, uint8_t btn, const Cause& cause) {
  if (learning()) return;
  for (uint8_t i = 0; i < cfg_.nrelays; i++) {
    const RelayCfg& r = cfg_.relay[i];
    for (uint8_t k = 0; k < r.ntrig; k++)
      if (r.trig[k].node == src && r.trig[k].btn == btn) {
        emitLightState(i, cause);
        break;
      }
  }
}

bool Node::evtDuplicate(NodeId src, uint16_t boot, uint8_t btn, uint8_t evt) {
  for (auto& e : evtSeen_) {
    if (e.valid && e.src == src && e.btn == btn) {
      // Na een herstart van de afzender begint evt opnieuw: de boot-waarde maakt het verschil.
      if (e.evt == evt && e.boot == boot) return true;
      e.evt = evt;
      e.boot = boot;
      return false;
    }
  }
  EvtSeen& e = evtSeen_[evtNext_];
  evtNext_ = uint8_t((evtNext_ + 1) % 24);
  e = {src, boot, btn, evt, true};
  return false;
}

void Node::button(uint8_t btn, bool pressed) {
  if (!pressed || btn >= cfg_.nbuttons || btn >= 8) return;
  uint32_t now = p_.nowMs();
  if (hasPressed_[btn] && now - lastPressAt_[btn] < t_.debounceMs) return;
  hasPressed_[btn] = true;
  lastPressAt_[btn] = now;
  Frame f = mk(Type::BtnEvent);
  uint8_t evt = ++evtCtr_[btn];
  Writer w(f.payload);
  w.u8(btn);
  w.u8(0);
  w.u8(evt);
  f.len = uint8_t(w.n);
  trigger(cfg_.id, btn, Cause{cfg_.id, btn, evt});  // lokaal en direct, zonder netwerk
  emit(f, t_.eventCopies, t_.eventGapMin, t_.eventGapMax, 0);
  // Indicator i hoort bij knop i: het licht dat die touch bedient. Wijzigt dat niet van staat, dan herhalen we.
  if (t_.ackRetries && btn < cfg_.nind && cfg_.follow[btn].valid && cfg_.follow[btn].node != cfg_.id && !learning())
    ack_[btn] = {true, 0, evt, now + t_.ackFirstMs};
  flush();
}

// ---------------------------------------------------------------- opdrachten / config

void Node::sendCmdLight(NodeId dst, uint8_t relay, Mode op) {
  Frame f = mk(Type::CmdLight);
  Writer w(f.payload);
  w.u16(dst);
  w.u8(relay);
  w.u8(uint8_t(op));
  f.len = uint8_t(w.n);
  emit(f, t_.eventCopies, t_.eventGapMin, t_.eventGapMax, 0);
  flush();
}

void Node::sendCfgTriggers(NodeId dst, uint8_t relay, const Trigger* t, uint8_t n) {
  Frame f = mk(Type::CfgSet);
  Writer w(f.payload);
  w.u16(dst);
  w.u8(0);
  w.u8(relay);
  w.u8(n);
  for (uint8_t i = 0; i < n; i++) {
    w.u16(t[i].node);
    w.u8(t[i].btn);
    w.u8(t[i].mode);
  }
  f.len = uint8_t(w.n);
  emit(f, t_.eventCopies, t_.eventGapMin, t_.eventGapMax, 0);
  flush();
}

void Node::sendCfgFollow(NodeId dst, uint8_t ind, NodeId node, uint8_t relay) {
  Frame f = mk(Type::CfgSet);
  Writer w(f.payload);
  w.u16(dst);
  w.u8(1);
  w.u8(ind);
  w.u8(node == ID_NONE ? 0 : 1);
  w.u16(node);
  w.u8(relay);
  f.len = uint8_t(w.n);
  emit(f, t_.eventCopies, t_.eventGapMin, t_.eventGapMax, 0);
  flush();
}

void Node::sendLearn(bool on) {
  Frame f = mk(Type::Learn);
  Writer w(f.payload);
  w.u8(on);
  f.len = uint8_t(w.n);
  emit(f, t_.eventCopies, t_.eventGapMin, t_.eventGapMax, 0);
  flush();
}

void Node::applyCfg(const Frame& f) {
  Reader r(f.payload, f.len);
  NodeId dst = r.u16();
  uint8_t kind = r.u8(), idx = r.u8(), n = r.u8();
  if (r.bad || dst != cfg_.id) return;
  if (kind == 0) {
    if (idx >= cfg_.nrelays || n > MAX_TRIG) return;
    Trigger t[MAX_TRIG];
    for (uint8_t i = 0; i < n; i++) {
      t[i].node = r.u16();
      t[i].btn = r.u8();
      t[i].mode = r.u8();
    }
    if (r.bad) return;
    for (uint8_t i = 0; i < n; i++) cfg_.relay[idx].trig[i] = t[i];
    cfg_.relay[idx].ntrig = n;
  } else if (kind == 1) {
    if (idx >= cfg_.nind || n > 1) return;
    FollowCfg& fc = cfg_.follow[idx];
    if (n == 0) {
      fc = {};
    } else {
      fc.node = r.u16();
      fc.relay = r.u8();
      if (r.bad) return;
      fc.valid = 1;
    }
    ind_[idx] = Ind{};
    if (fc.valid && fc.node == cfg_.id && fc.relay < cfg_.nrelays) noteState(cfg_.id, fc.relay, cfg_.relay[fc.relay].on, cfg_.relay[fc.relay].ver);
  } else {
    return;
  }
  p_.save(cfg_);
  Frame a = mk(Type::CfgAck);
  Writer w(a.payload);
  w.u16(f.id.src);
  w.u8(kind);
  w.u8(idx);
  w.u8(n);
  a.len = uint8_t(w.n);
  emit(a, 2, 10, 20, rnd(0, 10));
}

void Node::emitHeartbeat() {
  Frame f = mk(Type::Heartbeat);
  Writer w(f.payload);
  w.u16(cfg_.fw);
  w.u8(cfg_.flags);
  w.u8(cfg_.nrelays);
  for (uint8_t i = 0; i < cfg_.nrelays; i++) {
    w.u8(cfg_.relay[i].on);
    w.u16(cfg_.relay[i].ver);
  }
  // Achteraan toegevoegd (oudere lezers negeren het): wat de node heeft en een vingerafdruk van zijn config.
  w.u8(cfg_.nbuttons);
  w.u8(cfg_.nind);
  w.u16(configHash(cfg_));
  f.len = uint8_t(w.n);
  emit(f, 1, 0, 0, 0);
}

// ---------------------------------------------------------------- kanaal

void Node::emitChannelInfo(Type t, uint32_t delay) {
  Frame f = mk(t);
  Writer w(f.payload);
  w.u8(cfg_.channel);
  w.u16(cfg_.epoch);
  w.u8(uint8_t(isBridge() ? 1 : 0));
  f.len = uint8_t(w.n);
  emit(f, 1, 0, 0, delay);
}

void Node::noteChannel(uint8_t chan, uint16_t epoch, uint8_t flags) {
  if (!radioCapable() || isBridge()) return;  // een bridge volgt zijn wifi-AP, niemand anders
  if (scan_.active) {
    if (chan == scan_.ch) {
      if (!scan_.have || newer16(epoch, scan_.bestEpoch)) {
        scan_.have = true;
        scan_.bestCh = chan;
        scan_.bestEpoch = epoch;
        scan_.bridgeFound = (flags & 1) != 0;
      } else if (epoch == scan_.bestEpoch && chan == scan_.bestCh && (flags & 1)) {
        scan_.bridgeFound = true;
      }
    }
    return;
  }
  if (sw_.active) return;
  if (chan == cfg_.channel) {
    if (newer16(epoch, cfg_.epoch)) {
      cfg_.epoch = epoch;
      p_.save(cfg_);
    }
    return;
  }
  if (newer16(epoch, cfg_.epoch)) followChannel(chan, epoch);
}

// Kondig de verhuizing nog op het oude kanaal aan zodat buren meekomen, en schakel dan om.
void Node::followChannel(uint8_t ch, uint16_t epoch) {
  if (sw_.active) return;
  Frame f = mk(Type::Announce);
  Writer w(f.payload);
  w.u8(ch);
  w.u16(epoch);
  w.u8(uint8_t(isBridge() ? 1 : 0));
  f.len = uint8_t(w.n);
  emit(f, 3, 3, 5, 0);
  flush();
  sw_ = {true, ch, epoch, p_.nowMs() + t_.announceSwitchDelayMs};
  dropForwards();
}

void Node::doSwitch(uint8_t ch, uint16_t epoch) {
  dropForwards();
  cfg_.channel = ch;
  cfg_.epoch = epoch;
  p_.setChannel(ch);
  p_.save(cfg_);
  lastHeard_ = p_.nowMs();
  lastBeacon_ = lastHeard_;
  beaconLostCur_ = t_.beaconLostMs;
  nextBeacon_ = lastHeard_;
  stats.chSwitches++;
}

void Node::uplinkChannel(uint8_t ch, bool announceOnOld) {
  if (ch == cfg_.channel || !radioCapable()) return;
  uint16_t epoch = uint16_t(cfg_.epoch + 1);
  if (announceOnOld)
    followChannel(ch, epoch);
  else
    doSwitch(ch, epoch);
}

void Node::startScan() {
  stats.scans++;
  bootScanDone_ = true;
  scan_ = Scan{};
  scan_.active = true;
  scan_.dwellEnd = p_.nowMs();
  dropForwards();
}

void Node::stepScan() {
  uint32_t now = p_.nowMs();
  if (!due(now, scan_.dwellEnd)) return;
  if (++scan_.ch > 13) {
    finishScan();
    return;
  }
  p_.setChannel(scan_.ch);
  Frame f = mk(Type::Probe);
  emit(f, 2, t_.scanDwellMs / 3, t_.scanDwellMs / 3, 0);  // twee keer: een verloren probe kost anders een hele ronde
  flush();
  scan_.dwellEnd = now + t_.scanDwellMs;
}

void Node::finishScan() {
  uint32_t now = p_.nowMs();
  scan_.active = false;
  if (scan_.have) {
    if (scan_.bestCh == cfg_.channel) {
      p_.setChannel(cfg_.channel);
      if (newer16(scan_.bestEpoch, cfg_.epoch)) cfg_.epoch = scan_.bestEpoch;
      lastHeard_ = now;
      if (scan_.bridgeFound || !sawBeacon_) {
        // Het eiland leeft en de bridge is er (of er is geen bridge verwacht): rustig verder.
        lastBeacon_ = now;
        beaconLostCur_ = t_.beaconLostMs;
        scanFails_ = 0;
      } else if (++scanFails_ <= t_.scanQuickRetries) {
        // De bridge is niet gevonden: snel nog eens (de probe of het antwoord kan verloren zijn).
        uint32_t r = rnd(t_.scanRetryMin, t_.scanRetryMax);
        lastBeacon_ = now - (beaconLostCur_ > r ? beaconLostCur_ - r : 0);
      } else {
        // Bridge echt weg: steeds langer wachten tussen de zoektochten.
        lastBeacon_ = now;
        beaconLostCur_ = beaconLostCur_ * 2 > t_.beaconLostMax ? t_.beaconLostMax : beaconLostCur_ * 2;
      }
    } else {
      doSwitch(scan_.bestCh, scan_.bestEpoch);
      scanFails_ = 0;
    }
  } else {
    p_.setChannel(cfg_.channel);
    uint32_t r = rnd(t_.scanRetryMin, t_.scanRetryMax);
    lastHeard_ = now - (t_.lostMs - r);
    lastBeacon_ = now - (beaconLostCur_ > r ? beaconLostCur_ - r : 0);
  }
}

// ---------------------------------------------------------------- loop

void Node::serviceAcks() {
  uint32_t now = p_.nowMs();
  for (uint8_t b = 0; b < 8; b++) {
    AckWait& a = ack_[b];
    if (!a.active || !due(now, a.at)) continue;
    if (a.tries >= t_.ackRetries || learning()) {
      a.active = false;
      continue;
    }
    Frame f = mk(Type::BtnEvent);
    Writer w(f.payload);
    w.u8(b);
    w.u8(0);
    w.u8(a.evt);
    f.len = uint8_t(w.n);
    emit(f, 1, 0, 0, 0);
    a.tries++;
    a.at = now + (t_.ackFirstMs << a.tries);
    stats.ackRetries++;
  }
}

void Node::loop() {
  uint32_t now = p_.nowMs();
  flush();
  serviceAcks();
  if (sw_.active && due(now, sw_.at)) {
    sw_.active = false;
    doSwitch(sw_.ch, sw_.epoch);
  }
  if (!radioCapable()) return;
  if (scan_.active) {
    stepScan();
    return;
  }
  bool lostAll = int32_t(now - lastHeard_) > int32_t(t_.lostMs);
  bool lostBridge = sawBeacon_ && int32_t(now - lastBeacon_) > int32_t(beaconLostCur_);
  bool bootLost = !bootScanDone_ && !sawBeacon_ && int32_t(now - bootAt_) > int32_t(t_.bootScanMs);
  if (!isBridge() && !sw_.active && (lostAll || lostBridge || bootLost)) {
    startScan();
    return;
  }
  if (due(now, nextHb_)) {
    emitHeartbeat();
    if (hbFast_) {
      hbFast_--;
      nextHb_ = now + rnd(t_.hbFastMin, t_.hbFastMax);
    } else {
      nextHb_ = now + rnd(t_.hbMin, t_.hbMax);
    }
  }
  if (isBridge() && !sw_.active && due(now, nextBeacon_)) {
    emitChannelInfo(Type::Beacon, 0);
    nextBeacon_ = now + t_.beaconMs + rnd(0, 1000);
  }
}

}  // namespace kk
