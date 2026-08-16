#ifndef __TUNSTALL_BF16_H__
#define __TUNSTALL_BF16_H__

#include <cstddef>
#include <cstdint>
#include "tunstall.h"

// p_src 里有 n_bf16 个 BF16
// 把它们压缩到 p_dst 里，占 n_bf16 字节
// 压缩率固定为 2x
extern int TunstallCompressBF16(uint8_t* p_dst,  // 需要 n_bf16*2 字节, 但压缩后起始只有 n_bf16 字节
                                const uint16_t* p_src,  //      n_bf16*2 字节
                                size_t n_bf16);

// p_src 里是压缩流，占 n_bf16 字节
// 把它们解压到 p_dst ，解压出来 n_bf16 个 BF16
extern int TunstallDecompressBF16(uint16_t* p_dst,       //      n_bf16*2 字节
                                  const uint8_t* p_src,  //      n_bf16 字节
                                  size_t n_bf16);

// In-place BF16 decompression.
// Before: p_data[0:n_bf16] contains compressed bytes and p_data[0:n_bf16*2] is writable.
// After : p_data[0:n_bf16*2] contains n_bf16 BF16 values.
extern int TunstallDecompressBF16Inplace(
    uint8_t* p_data,  // 可访问范围是 n_bf16*2 字节，解压前的数据占据 p_data[0:n_bf16]
                      // 字节，解压后占据 p_data[0:n_bf16*2] 字节
    size_t n_bf16);

// ===== FP8 (E5M2) lossy 2x compression — round-11e Tier-2 bounded-loss re-frame =====
// Drop-in lossy alternative to Tunstall R200: 1 byte per BF16 value (2:1 byte reduction),
// directly attacking the overflow-disk-read wall (stored KV bytes halved). Quality guard
// (perplexity / output-match tolerance) is enforced at the eval layer, not here.
// p_src: n_bf16 BF16 values (n_bf16*2 bytes). p_dst: n_bf16 FP8 bytes (1 byte/value).
extern void CompressBF16ToFP8(uint8_t* p_dst, const uint16_t* p_src, size_t n_bf16);

// In-place FP8->BF16 decompression (1:2 expand, NEON-vectorized, AIV-trivial).
// Before: p_data[0:n_bf16] holds FP8 bytes; p_data[0:n_bf16*2] is writable.
// After : p_data[0:n_bf16*2] holds n_bf16 BF16 values. Iterates backward so the
// 1:2 expansion never overwrites unread FP8 source.
extern int DecompressFP8ToBF16Inplace(uint8_t* p_data, size_t n_bf16);

#endif  // __TUNSTALL_BF16_H__