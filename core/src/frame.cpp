#include "kk/frame.h"

#include <string.h>

extern "C" {
#include "monocypher.h"
}

namespace kk {

static void makeNonce(uint8_t n[24], uint8_t type, NodeId src, uint16_t boot, uint16_t ctr) {
  memset(n, 0, 24);
  n[0] = uint8_t(src);
  n[1] = uint8_t(src >> 8);
  n[2] = uint8_t(boot);
  n[3] = uint8_t(boot >> 8);
  n[4] = uint8_t(ctr);
  n[5] = uint8_t(ctr >> 8);
  n[6] = type;
}

static void makeAd(uint8_t ad[8], uint8_t type, NodeId src, uint16_t boot, uint16_t ctr) {
  ad[0] = PROTO_VER;
  ad[1] = type;
  ad[2] = uint8_t(src);
  ad[3] = uint8_t(src >> 8);
  ad[4] = uint8_t(boot);
  ad[5] = uint8_t(boot >> 8);
  ad[6] = uint8_t(ctr);
  ad[7] = uint8_t(ctr >> 8);
}

size_t seal(const Key& k, const Frame& f, uint8_t* out) {
  if (f.len > MAX_PAYLOAD) return 0;
  out[0] = PROTO_VER;
  out[1] = uint8_t(f.type);
  out[2] = f.ttl;
  out[3] = uint8_t(f.id.src);
  out[4] = uint8_t(f.id.src >> 8);
  out[5] = uint8_t(f.id.boot);
  out[6] = uint8_t(f.id.boot >> 8);
  out[7] = uint8_t(f.id.ctr);
  out[8] = uint8_t(f.id.ctr >> 8);
  uint8_t nonce[24], ad[8];
  makeNonce(nonce, uint8_t(f.type), f.id.src, f.id.boot, f.id.ctr);
  makeAd(ad, uint8_t(f.type), f.id.src, f.id.boot, f.id.ctr);
  crypto_aead_lock(out + HDR, out + HDR + f.len, k.b, nonce, ad, sizeof ad, f.payload, f.len);
  return HDR + f.len + TAG;
}

bool open(const Key& k, const uint8_t* in, size_t n, Frame& f) {
  if (n < HDR + TAG || n > MAX_FRAME) return false;
  if (in[0] != PROTO_VER) return false;
  f.type = Type(in[1]);
  f.ttl = in[2];
  f.id.src = NodeId(in[3] | (in[4] << 8));
  f.id.boot = uint16_t(in[5] | (in[6] << 8));
  f.id.ctr = uint16_t(in[7] | (in[8] << 8));
  f.len = uint8_t(n - HDR - TAG);
  uint8_t nonce[24], ad[8];
  makeNonce(nonce, in[1], f.id.src, f.id.boot, f.id.ctr);
  makeAd(ad, in[1], f.id.src, f.id.boot, f.id.ctr);
  return crypto_aead_unlock(f.payload, in + HDR + f.len, k.b, nonce, ad, sizeof ad, in + HDR, f.len) == 0;
}

}  // namespace kk
