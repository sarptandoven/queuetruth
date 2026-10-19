//! Minimal, dependency-free ZIP writer for the `.npz` export: STORED members only (no compression), CRC-32
//! (IEEE 802.3, reflected polynomial `0xEDB88320`) computed in-crate, local file headers, central directory and
//! end-of-central-directory record (APPNOTE 6.3.10). Every timestamp is the DOS epoch 1980-01-01 00:00:00 and no
//! extra time fields are written, so equal inputs give byte-identical archives. ZIP64 fields are written only when
//! a member is 4 GiB or larger (or an offset or count overflows the 32/16-bit fields); this writer never extracts.

use std::io::{self, Write};

/// Sentinel of a 32-bit field whose value lives in the ZIP64 extra field / record.
const U32_SENTINEL: u64 = 0xFFFF_FFFF;
/// Sentinel of a 16-bit entry count whose value lives in the ZIP64 end-of-central-directory record.
const U16_SENTINEL: u64 = 0xFFFF;
/// DOS time 00:00:00.
const DOS_TIME: u16 = 0;
/// DOS date 1980-01-01 (`(year - 1980) << 9 | month << 5 | day`).
const DOS_DATE: u16 = (1 << 5) | 1;
/// "Version needed to extract": 2.0 (STORED) and 4.5 (ZIP64).
const VERSION_20: u16 = 20;
const VERSION_45: u16 = 45;
/// "Version made by": host 3 (UNIX) in the high byte, so the external attributes carry the file mode.
const MADE_BY_UNIX: u16 = 3 << 8;
/// External attributes: regular file, mode 0644.
const EXTERNAL_ATTRS: u32 = 0o100_644 << 16;

const fn crc_tables() -> [[u32; 256]; 8] {
    let mut t = [[0u32; 256]; 8];
    let mut i = 0;
    while i < 256 {
        let mut c = i as u32;
        let mut k = 0;
        while k < 8 {
            c = if c & 1 != 0 { 0xEDB8_8320 ^ (c >> 1) } else { c >> 1 };
            k += 1;
        }
        t[0][i] = c;
        i += 1;
    }
    let mut i = 0;
    while i < 256 {
        let mut s = 1;
        while s < 8 {
            let prev = t[s - 1][i];
            t[s][i] = (prev >> 8) ^ t[0][(prev & 0xFF) as usize];
            s += 1;
        }
        i += 1;
    }
    t
}

/// Slicing-by-8 tables (8 KiB, built at compile time).
static CRC_TABLES: [[u32; 256]; 8] = crc_tables();

/// CRC-32 (IEEE, as used by ZIP, gzip and PNG) of `data`.
pub(crate) fn crc32(data: &[u8]) -> u32 {
    let t = &CRC_TABLES;
    let mut c = !0u32;
    let mut chunks = data.chunks_exact(8);
    for ch in &mut chunks {
        let lo = c ^ u32::from_le_bytes([ch[0], ch[1], ch[2], ch[3]]);
        c = t[7][(lo & 0xFF) as usize]
            ^ t[6][((lo >> 8) & 0xFF) as usize]
            ^ t[5][((lo >> 16) & 0xFF) as usize]
            ^ t[4][(lo >> 24) as usize]
            ^ t[3][usize::from(ch[4])]
            ^ t[2][usize::from(ch[5])]
            ^ t[1][usize::from(ch[6])]
            ^ t[0][usize::from(ch[7])];
    }
    for &b in chunks.remainder() {
        c = (c >> 8) ^ t[0][((c ^ u32::from(b)) & 0xFF) as usize];
    }
    !c
}

/// Little-endian field writer that counts the bytes it wrote.
struct Out<W: Write> {
    w: W,
    pos: u64,
}

impl<W: Write> Out<W> {
    fn bytes(&mut self, b: &[u8]) -> io::Result<()> {
        self.w.write_all(b)?;
        self.pos += b.len() as u64;
        Ok(())
    }
    fn u16(&mut self, v: u16) -> io::Result<()> {
        self.bytes(&v.to_le_bytes())
    }
    fn u32(&mut self, v: u32) -> io::Result<()> {
        self.bytes(&v.to_le_bytes())
    }
    fn u64(&mut self, v: u64) -> io::Result<()> {
        self.bytes(&v.to_le_bytes())
    }
}

/// `v` as a 32-bit field, or the sentinel when it does not fit (or ZIP64 is forced for the field).
fn field32(v: u64, zip64: bool) -> u32 {
    if zip64 || v >= U32_SENTINEL {
        u32::MAX
    } else {
        // Fits: checked just above.
        u32::try_from(v).unwrap_or(u32::MAX)
    }
}

fn invalid(msg: String) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidInput, msg)
}

/// Where each member's local header starts, and the member's size / CRC.
struct Placed<'a> {
    name: &'a str,
    size: u64,
    crc: u32,
    offset: u64,
    /// Sizes go to the ZIP64 extra field.
    big: bool,
}

/// Writes a ZIP archive of STORED `members` (`(name, bytes)`, in order) to `w`; returns the archive length.
/// `force_zip64` writes the ZIP64 fields even when nothing overflows (tests only).
pub(crate) fn write_stored_zip<W: Write>(w: W, members: &[(&str, &[u8])], force_zip64: bool) -> io::Result<u64> {
    let mut o = Out { w, pos: 0 };
    let mut placed = Vec::with_capacity(members.len());
    for &(name, data) in members {
        let name_len =
            u16::try_from(name.len()).map_err(|_| invalid(format!("zip member name of {} bytes", name.len())))?;
        if name.is_empty() || name.starts_with('/') || name.contains('\\') {
            return Err(invalid(format!("zip member name {name:?}")));
        }
        let size = data.len() as u64;
        let big = force_zip64 || size >= U32_SENTINEL;
        let p = Placed {
            name,
            size,
            crc: crc32(data),
            offset: o.pos,
            big,
        };
        // Local file header.
        o.u32(0x0403_4b50)?;
        o.u16(if big { VERSION_45 } else { VERSION_20 })?;
        o.u16(0)?; // flags: sizes known up front, no data descriptor; names are ASCII
        o.u16(0)?; // method: STORED
        o.u16(DOS_TIME)?;
        o.u16(DOS_DATE)?;
        o.u32(p.crc)?;
        o.u32(field32(size, big))?; // compressed size
        o.u32(field32(size, big))?; // uncompressed size
        o.u16(name_len)?;
        o.u16(if big { 20 } else { 0 })?;
        o.bytes(name.as_bytes())?;
        if big {
            o.u16(0x0001)?;
            o.u16(16)?;
            o.u64(size)?; // uncompressed
            o.u64(size)?; // compressed
        }
        o.bytes(data)?;
        placed.push(p);
    }
    let cd_offset = o.pos;
    for p in &placed {
        let far = force_zip64 || p.offset >= U32_SENTINEL;
        let z64_len: u16 = if p.big { 16 } else { 0 } + if far { 8 } else { 0 };
        let extra = if z64_len > 0 { 4 + z64_len } else { 0 };
        let needed = if p.big || far { VERSION_45 } else { VERSION_20 };
        o.u32(0x0201_4b50)?;
        o.u16(MADE_BY_UNIX | needed)?;
        o.u16(needed)?;
        o.u16(0)?; // flags
        o.u16(0)?; // method: STORED
        o.u16(DOS_TIME)?;
        o.u16(DOS_DATE)?;
        o.u32(p.crc)?;
        o.u32(field32(p.size, p.big))?;
        o.u32(field32(p.size, p.big))?;
        o.u16(u16::try_from(p.name.len()).unwrap_or(u16::MAX))?;
        o.u16(extra)?;
        o.u16(0)?; // comment length
        o.u16(0)?; // disk number start
        o.u16(0)?; // internal attributes
        o.u32(EXTERNAL_ATTRS)?;
        o.u32(field32(p.offset, far))?;
        o.bytes(p.name.as_bytes())?;
        if extra > 0 {
            // ZIP64 extended information: only the fields whose header value is the sentinel, in this order.
            o.u16(0x0001)?;
            o.u16(z64_len)?;
            if p.big {
                o.u64(p.size)?; // uncompressed
                o.u64(p.size)?; // compressed
            }
            if far {
                o.u64(p.offset)?;
            }
        }
    }
    let cd_size = o.pos - cd_offset;
    let n = placed.len() as u64;
    let zip64_end = force_zip64 || n >= U16_SENTINEL || cd_size >= U32_SENTINEL || cd_offset >= U32_SENTINEL;
    if zip64_end {
        let rec_offset = o.pos;
        // ZIP64 end of central directory record (size field excludes its leading 12 bytes).
        o.u32(0x0606_4b50)?;
        o.u64(44)?;
        o.u16(MADE_BY_UNIX | VERSION_45)?;
        o.u16(VERSION_45)?;
        o.u32(0)?; // this disk
        o.u32(0)?; // disk with the central directory
        o.u64(n)?;
        o.u64(n)?;
        o.u64(cd_size)?;
        o.u64(cd_offset)?;
        // ZIP64 end of central directory locator.
        o.u32(0x0706_4b50)?;
        o.u32(0)?;
        o.u64(rec_offset)?;
        o.u32(1)?; // total disks
    }
    let n16 = if zip64_end || n >= U16_SENTINEL {
        u16::MAX
    } else {
        u16::try_from(n).unwrap_or(u16::MAX)
    };
    o.u32(0x0605_4b50)?;
    o.u16(0)?;
    o.u16(0)?;
    o.u16(n16)?;
    o.u16(n16)?;
    o.u32(field32(cd_size, zip64_end))?;
    o.u32(field32(cd_offset, zip64_end))?;
    o.u16(0)?; // comment length
    o.w.flush()?;
    Ok(o.pos)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn crc32_bitwise(data: &[u8]) -> u32 {
        let mut c = !0u32;
        for &b in data {
            c ^= u32::from(b);
            for _ in 0..8 {
                c = if c & 1 != 0 { 0xEDB8_8320 ^ (c >> 1) } else { c >> 1 };
            }
        }
        !c
    }

    #[test]
    fn crc32_known_answers() {
        // Check values of CRC-32/ISO-HDLC (the "check" string of the CRC catalogue and common vectors).
        assert_eq!(crc32(b""), 0);
        assert_eq!(crc32(b"a"), 0xE8B7_BE43);
        assert_eq!(crc32(b"abc"), 0x3524_41C2);
        assert_eq!(crc32(b"123456789"), 0xCBF4_3926);
        assert_eq!(crc32(b"The quick brown fox jumps over the lazy dog"), 0x414F_A339);
        assert_eq!(crc32(&[0u8; 32]), 0x190A_55AD);
        assert_eq!(crc32(&[0xFFu8; 32]), 0xFF6C_AB0B);
        // Slicing-by-8 against the bitwise definition on every length / alignment of the remainder loop.
        let data: Vec<u8> = (0u32..4099)
            .map(|i| (i.wrapping_mul(2_654_435_761) >> 13) as u8)
            .collect();
        for n in (0..64).chain([1000, 4095, 4096, 4099]) {
            for start in 0..3 {
                let s = &data[start..start + n.min(data.len() - start)];
                assert_eq!(crc32(s), crc32_bitwise(s), "len {} start {start}", s.len());
            }
        }
    }

    fn le16(b: &[u8], o: usize) -> u64 {
        u64::from(u16::from_le_bytes([b[o], b[o + 1]]))
    }
    fn le32(b: &[u8], o: usize) -> u64 {
        u64::from(u32::from_le_bytes(b[o..o + 4].try_into().unwrap()))
    }
    fn le64(b: &[u8], o: usize) -> u64 {
        u64::from_le_bytes(b[o..o + 8].try_into().unwrap())
    }
    fn us(v: u64) -> usize {
        usize::try_from(v).unwrap()
    }

    /// A strict reader of what `write_stored_zip` produces: walks EOCD (and the ZIP64 locator / record when present)
    /// to the central directory, resolves ZIP64 extra fields, and checks each local header, size and CRC.
    fn read_back(z: &[u8]) -> Vec<(String, Vec<u8>)> {
        let eocd = z.len() - 22;
        assert_eq!(le32(z, eocd), 0x0605_4b50);
        assert_eq!(le16(z, eocd + 20), 0, "no comment");
        let (mut n, mut cd_size, mut cd_off) = (le16(z, eocd + 10), le32(z, eocd + 12), le32(z, eocd + 16));
        if n == 0xFFFF || cd_size == 0xFFFF_FFFF || cd_off == 0xFFFF_FFFF {
            let loc = eocd - 20;
            assert_eq!(le32(z, loc), 0x0706_4b50, "ZIP64 locator");
            let rec = us(le64(z, loc + 8));
            assert_eq!(rec + 56, loc);
            assert_eq!(le32(z, rec), 0x0606_4b50, "ZIP64 EOCD record");
            assert_eq!(le64(z, rec + 4), 44);
            assert_eq!(le64(z, rec + 24), le64(z, rec + 32));
            (n, cd_size, cd_off) = (le64(z, rec + 32), le64(z, rec + 40), le64(z, rec + 48));
            assert_eq!(cd_off + cd_size, rec as u64);
        } else {
            assert_eq!(cd_off + cd_size, eocd as u64);
        }
        let mut out = Vec::new();
        let mut p = us(cd_off);
        let mut expect_local = 0u64;
        for _ in 0..n {
            assert_eq!(le32(z, p), 0x0201_4b50);
            assert_eq!(le16(z, p + 10), 0, "STORED");
            assert_eq!((le16(z, p + 12), le16(z, p + 14)), (0, 0x21));
            let crc = le32(z, p + 16);
            let (mut csize, mut usize_, mut off) = (le32(z, p + 20), le32(z, p + 24), le32(z, p + 42));
            let (nl, xl, cl) = (us(le16(z, p + 28)), us(le16(z, p + 30)), us(le16(z, p + 32)));
            assert_eq!(cl, 0);
            let name = String::from_utf8(z[p + 46..p + 46 + nl].to_vec()).unwrap();
            let mut x = p + 46 + nl;
            if xl > 0 {
                assert_eq!(le16(z, x), 1, "ZIP64 extra");
                let mut len = le16(z, x + 2);
                x += 4;
                assert_eq!(us(len) + 4, xl);
                for v in [&mut usize_, &mut csize, &mut off] {
                    if *v == 0xFFFF_FFFF {
                        *v = le64(z, x);
                        x += 8;
                        len -= 8;
                    }
                }
                assert_eq!(len, 0, "no unused ZIP64 bytes");
            }
            assert_eq!(csize, usize_);
            assert_eq!(off, expect_local, "members are contiguous");
            let l = us(off);
            assert_eq!(le32(z, l), 0x0403_4b50);
            assert_eq!(le16(z, l + 8), 0);
            assert_eq!(le32(z, l + 14), crc);
            let lnl = us(le16(z, l + 26));
            let lxl = us(le16(z, l + 28));
            assert_eq!(&z[l + 30..l + 30 + lnl], name.as_bytes());
            if lxl > 0 {
                assert_eq!((le32(z, l + 18), le32(z, l + 22)), (0xFFFF_FFFF, 0xFFFF_FFFF));
                assert_eq!((le16(z, l + 30 + lnl), le16(z, l + 32 + lnl)), (1, 16));
                assert_eq!(le64(z, l + 34 + lnl), usize_);
                assert_eq!(le64(z, l + 42 + lnl), csize);
            } else {
                assert_eq!((le32(z, l + 18), le32(z, l + 22)), (csize, usize_));
            }
            let d = l + 30 + lnl + lxl;
            let data = z[d..d + us(csize)].to_vec();
            assert_eq!(u64::from(crc32_bitwise(&data)), crc);
            expect_local = (d + data.len()) as u64;
            out.push((name, data));
            p = x + cl;
        }
        assert_eq!(expect_local, cd_off, "central directory follows the last member");
        assert_eq!(p as u64, cd_off + cd_size);
        out
    }

    fn zip_bytes(members: &[(&str, &[u8])], force_zip64: bool) -> Vec<u8> {
        let mut v = Vec::new();
        let n = write_stored_zip(&mut v, members, force_zip64).unwrap();
        assert_eq!(n, v.len() as u64);
        v
    }

    #[test]
    fn layout_round_trips_and_is_byte_deterministic() {
        let big: Vec<u8> = (0u32..100_000).map(|i| (i % 251) as u8).collect();
        let members: [(&str, &[u8]); 3] = [("data.npy", &big), ("empty.npy", b""), ("b/c.txt", b"hello")];
        for force in [false, true] {
            let a = zip_bytes(&members, force);
            assert_eq!(a, zip_bytes(&members, force), "byte-deterministic");
            let back = read_back(&a);
            let want: Vec<(String, Vec<u8>)> = members.iter().map(|(n, d)| (n.to_string(), d.to_vec())).collect();
            assert_eq!(back, want, "force_zip64 {force}");
        }
        // Exact size of the plain layout: 30 + name + data per local header, 46 + name per central entry, 22 EOCD.
        let a = zip_bytes(&members[..1], false);
        assert_eq!(a.len(), 30 + 8 + big.len() + 46 + 8 + 22);
        assert_eq!(&a[..4], b"PK\x03\x04");
        // Empty archive: just the EOCD.
        assert_eq!(
            zip_bytes(&[], false),
            b"PK\x05\x06\0\0\0\0\0\0\0\0\0\0\0\0\0\0\0\0\0\0".to_vec()
        );
    }

    #[test]
    fn golden_bytes_of_a_tiny_archive() {
        let z = zip_bytes(&[("a", b"x")], false);
        let mut want = Vec::new();
        // Local header: sig, ver 20, flags 0, STORED, time 0, date 0x21, crc("x"), sizes 1/1, name len 1, extra 0.
        want.extend_from_slice(b"PK\x03\x04\x14\x00\x00\x00\x00\x00\x00\x00\x21\x00");
        want.extend_from_slice(&crc32(b"x").to_le_bytes());
        want.extend_from_slice(b"\x01\x00\x00\x00\x01\x00\x00\x00\x01\x00\x00\x00ax");
        // Central entry: made by UNIX 2.0, needed 2.0, ..., external attrs 0644 regular file, offset 0.
        want.extend_from_slice(b"PK\x01\x02\x14\x03\x14\x00\x00\x00\x00\x00\x00\x00\x21\x00");
        want.extend_from_slice(&crc32(b"x").to_le_bytes());
        want.extend_from_slice(b"\x01\x00\x00\x00\x01\x00\x00\x00\x01\x00\x00\x00\x00\x00\x00\x00\x00\x00");
        want.extend_from_slice(&(0o100_644u32 << 16).to_le_bytes());
        want.extend_from_slice(b"\x00\x00\x00\x00a");
        // EOCD: 1 entry, central directory of 47 bytes at offset 32.
        want.extend_from_slice(b"PK\x05\x06\x00\x00\x00\x00\x01\x00\x01\x00\x2f\x00\x00\x00\x20\x00\x00\x00\x00\x00");
        assert_eq!(z, want);
        assert_eq!(crc32(b"x"), 0x8CDC_1683);
    }

    #[test]
    fn bad_member_names_are_refused() {
        for name in ["", "/abs", "a\\b"] {
            let e = write_stored_zip(Vec::new(), &[(name, b"x")], false).unwrap_err();
            assert_eq!(e.kind(), io::ErrorKind::InvalidInput);
        }
        let long = "n".repeat(70_000);
        assert!(write_stored_zip(Vec::new(), &[(long.as_str(), b"x")], false).is_err());
    }

    /// Archives for the manual numpy / zipfile check (`np.load(p)["data"]`), in the OS temp dir.
    #[test]
    fn writes_npz_samples_for_numpy() {
        let events: Vec<u8> = (0u8..=255).cycle().take(3 * crate::EVENT_SIZE).collect();
        let npy = crate::npy_bytes(&events);
        let dir = std::env::temp_dir().join("qt-l2-zipw");
        std::fs::create_dir_all(&dir).unwrap();
        for (file, force) in [("plain.npz", false), ("zip64.npz", true)] {
            let f = std::fs::File::create(dir.join(file)).unwrap();
            write_stored_zip(f, &[("data.npy", &npy)], force).unwrap();
            let z = std::fs::read(dir.join(file)).unwrap();
            assert_eq!(read_back(&z), vec![("data.npy".to_string(), npy.clone())]);
        }
    }
}
