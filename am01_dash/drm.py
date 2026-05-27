"""Minimal DRM uAPI bindings via ctypes/ioctl for ms912x KMS-only card.

只实现独占 KMS-only 卡所需的最少 ioctl 集合，足以驱动 ms912x 副屏。
参考：include/uapi/drm/drm.h + drm_mode.h
"""
import ctypes as C
import fcntl
import mmap
import os
from dataclasses import dataclass
from typing import List, Optional

# ============ ioctl helpers ============
_IOC_NRBITS, _IOC_TYPEBITS = 8, 8
_IOC_SIZEBITS, _IOC_DIRBITS = 14, 2
_IOC_NRSHIFT = 0
_IOC_TYPESHIFT = _IOC_NRSHIFT + _IOC_NRBITS
_IOC_SIZESHIFT = _IOC_TYPESHIFT + _IOC_TYPEBITS
_IOC_DIRSHIFT = _IOC_SIZESHIFT + _IOC_SIZEBITS
_IOC_NONE, _IOC_WRITE, _IOC_READ = 0, 1, 2

def _IOC(d, t, nr, sz):
    return (d << _IOC_DIRSHIFT) | (t << _IOC_TYPESHIFT) | (nr << _IOC_NRSHIFT) | (sz << _IOC_SIZESHIFT)
def _IO(t, nr):      return _IOC(_IOC_NONE,  t, nr, 0)
def _IOR(t, nr, s):  return _IOC(_IOC_READ,  t, nr, C.sizeof(s))
def _IOW(t, nr, s):  return _IOC(_IOC_WRITE, t, nr, C.sizeof(s))
def _IOWR(t, nr, s): return _IOC(_IOC_READ | _IOC_WRITE, t, nr, C.sizeof(s))

DRM_IOCTL_BASE = ord('d')


# ============ DRM structs (drm_mode.h) ============

class drm_mode_card_res(C.Structure):
    _fields_ = [
        ("fb_id_ptr", C.c_uint64),
        ("crtc_id_ptr", C.c_uint64),
        ("connector_id_ptr", C.c_uint64),
        ("encoder_id_ptr", C.c_uint64),
        ("count_fbs", C.c_uint32),
        ("count_crtcs", C.c_uint32),
        ("count_connectors", C.c_uint32),
        ("count_encoders", C.c_uint32),
        ("min_width", C.c_uint32),
        ("max_width", C.c_uint32),
        ("min_height", C.c_uint32),
        ("max_height", C.c_uint32),
    ]


DRM_DISPLAY_MODE_LEN = 32


class drm_mode_modeinfo(C.Structure):
    _fields_ = [
        ("clock", C.c_uint32),
        ("hdisplay", C.c_uint16),
        ("hsync_start", C.c_uint16),
        ("hsync_end", C.c_uint16),
        ("htotal", C.c_uint16),
        ("hskew", C.c_uint16),
        ("vdisplay", C.c_uint16),
        ("vsync_start", C.c_uint16),
        ("vsync_end", C.c_uint16),
        ("vtotal", C.c_uint16),
        ("vscan", C.c_uint16),
        ("vrefresh", C.c_uint32),
        ("flags", C.c_uint32),
        ("type", C.c_uint32),
        ("name", C.c_char * DRM_DISPLAY_MODE_LEN),
    ]


class drm_mode_get_connector(C.Structure):
    _fields_ = [
        ("encoders_ptr", C.c_uint64),
        ("modes_ptr", C.c_uint64),
        ("props_ptr", C.c_uint64),
        ("prop_values_ptr", C.c_uint64),
        ("count_modes", C.c_uint32),
        ("count_props", C.c_uint32),
        ("count_encoders", C.c_uint32),
        ("encoder_id", C.c_uint32),
        ("connector_id", C.c_uint32),
        ("connector_type", C.c_uint32),
        ("connector_type_id", C.c_uint32),
        ("connection", C.c_uint32),
        ("mm_width", C.c_uint32),
        ("mm_height", C.c_uint32),
        ("subpixel", C.c_uint32),
        ("pad", C.c_uint32),
    ]


class drm_mode_get_encoder(C.Structure):
    _fields_ = [
        ("encoder_id", C.c_uint32),
        ("encoder_type", C.c_uint32),
        ("crtc_id", C.c_uint32),
        ("possible_crtcs", C.c_uint32),
        ("possible_clones", C.c_uint32),
    ]


class drm_mode_create_dumb(C.Structure):
    _fields_ = [
        ("height", C.c_uint32),
        ("width", C.c_uint32),
        ("bpp", C.c_uint32),
        ("flags", C.c_uint32),
        ("handle", C.c_uint32),
        ("pitch", C.c_uint32),
        ("size", C.c_uint64),
    ]


class drm_mode_map_dumb(C.Structure):
    _fields_ = [
        ("handle", C.c_uint32),
        ("pad", C.c_uint32),
        ("offset", C.c_uint64),
    ]


class drm_mode_destroy_dumb(C.Structure):
    _fields_ = [("handle", C.c_uint32)]


class drm_mode_fb_cmd(C.Structure):
    _fields_ = [
        ("fb_id", C.c_uint32),
        ("width", C.c_uint32),
        ("height", C.c_uint32),
        ("pitch", C.c_uint32),
        ("bpp", C.c_uint32),
        ("depth", C.c_uint32),
        ("handle", C.c_uint32),
    ]


class drm_mode_crtc(C.Structure):
    _fields_ = [
        ("set_connectors_ptr", C.c_uint64),
        ("count_connectors", C.c_uint32),
        ("crtc_id", C.c_uint32),
        ("fb_id", C.c_uint32),
        ("x", C.c_uint32),
        ("y", C.c_uint32),
        ("gamma_size", C.c_uint32),
        ("mode_valid", C.c_uint32),
        ("mode", drm_mode_modeinfo),
    ]


# ============ ioctl numbers ============

DRM_IOCTL_SET_MASTER          = _IO(DRM_IOCTL_BASE, 0x1e)
DRM_IOCTL_DROP_MASTER         = _IO(DRM_IOCTL_BASE, 0x1f)
DRM_IOCTL_MODE_GETRESOURCES   = _IOWR(DRM_IOCTL_BASE, 0xA0, drm_mode_card_res)
DRM_IOCTL_MODE_GETCRTC        = _IOWR(DRM_IOCTL_BASE, 0xA1, drm_mode_crtc)
DRM_IOCTL_MODE_SETCRTC        = _IOWR(DRM_IOCTL_BASE, 0xA2, drm_mode_crtc)
DRM_IOCTL_MODE_GETENCODER     = _IOWR(DRM_IOCTL_BASE, 0xA6, drm_mode_get_encoder)
DRM_IOCTL_MODE_GETCONNECTOR   = _IOWR(DRM_IOCTL_BASE, 0xA7, drm_mode_get_connector)
DRM_IOCTL_MODE_ADDFB          = _IOWR(DRM_IOCTL_BASE, 0xAE, drm_mode_fb_cmd)
DRM_IOCTL_MODE_RMFB           = _IOWR(DRM_IOCTL_BASE, 0xAF, C.c_uint32)
DRM_IOCTL_MODE_CREATE_DUMB    = _IOWR(DRM_IOCTL_BASE, 0xB2, drm_mode_create_dumb)
DRM_IOCTL_MODE_MAP_DUMB       = _IOWR(DRM_IOCTL_BASE, 0xB3, drm_mode_map_dumb)
DRM_IOCTL_MODE_DESTROY_DUMB   = _IOWR(DRM_IOCTL_BASE, 0xB4, drm_mode_destroy_dumb)


# PAGE_FLIP（异步翻页，会触发 pipe_update 路径，关键）
class drm_mode_crtc_page_flip(C.Structure):
    _fields_ = [
        ("crtc_id", C.c_uint32),
        ("fb_id", C.c_uint32),
        ("flags", C.c_uint32),
        ("reserved", C.c_uint32),
        ("user_data", C.c_uint64),
    ]

DRM_IOCTL_MODE_PAGE_FLIP      = _IOWR(DRM_IOCTL_BASE, 0xB0, drm_mode_crtc_page_flip)

# DRM_MODE_PAGE_FLIP_EVENT = 0x01  (不用 event，省得读 fd)
# 注意：legacy page flip 无 flag = 同步语义（提交后立即返回，下一次 vblank 生效）


# ATOMIC commit —— 真正能触发 simple_display_pipe update 的 modern 路径
class drm_mode_atomic(C.Structure):
    _fields_ = [
        ("flags", C.c_uint32),
        ("count_objs", C.c_uint32),
        ("objs_ptr", C.c_uint64),
        ("count_props_ptr", C.c_uint64),
        ("props_ptr", C.c_uint64),
        ("prop_values_ptr", C.c_uint64),
        ("reserved", C.c_uint64),
        ("user_data", C.c_uint64),
    ]

DRM_IOCTL_MODE_ATOMIC         = _IOWR(DRM_IOCTL_BASE, 0xBC, drm_mode_atomic)


# 让 DRM master 允许使用 atomic（CAP）
class drm_set_client_cap(C.Structure):
    _fields_ = [
        ("capability", C.c_uint64),
        ("value", C.c_uint64),
    ]

DRM_IOCTL_SET_CLIENT_CAP      = _IOW(DRM_IOCTL_BASE, 0x0d, drm_set_client_cap)

DRM_CLIENT_CAP_UNIVERSAL_PLANES = 2
DRM_CLIENT_CAP_ATOMIC           = 3


DRM_MODE_CONNECTED = 1


# ============ helpers ============

def _ioctl(fd: int, request: int, arg) -> None:
    """带 EINTR 重试的 ioctl。arg 必须是 ctypes Structure。"""
    while True:
        try:
            fcntl.ioctl(fd, request & 0xFFFFFFFF, arg)
            return
        except InterruptedError:
            continue


# ============ data classes ============

@dataclass
class ConnectorInfo:
    id: int
    connection: int             # 1 = connected
    encoder_id: int             # 当前活跃 encoder, 0 if none
    modes: List[drm_mode_modeinfo]
    mm_width: int
    mm_height: int
    possible_encoders: List[int]  # 该 connector 支持的所有 encoder id


@dataclass
class EncoderInfo:
    id: int
    crtc_id: int                # 0 if none
    possible_crtcs: int


@dataclass
class DumbBuffer:
    handle: int
    pitch: int
    size: int
    fb_id: int
    map_offset: int
    mmap_obj: Optional[mmap.mmap] = None


# ============ DRM device ============

class DrmDevice:
    """
    独占管理 ms912x 副屏对应的 KMS-only DRM card。

    用法（推荐 with 块）：
        with DrmDevice("/dev/dri/card0") as dev:
            dev.modeset()                      # 自动找 connector / encoder / crtc / mode
            buf = dev.create_framebuffer()     # 创建一块 XRGB8888 dumb buffer
            buf.mmap_obj[:] = rgb_bytes        # 写像素
            dev.flip_to(buf)                   # 推到屏幕
    """

    def __init__(self, path: str = "/dev/dri/card0"):
        self.path = path
        self.fd: int = -1
        self.is_master = False
        # 由 modeset() 填充
        self.connector: Optional[ConnectorInfo] = None
        self.encoder: Optional[EncoderInfo] = None
        self.crtc_id: int = 0
        self.mode: Optional[drm_mode_modeinfo] = None
        self.width = 0
        self.height = 0
        self._buffers: List[DumbBuffer] = []

    # ---- context manager ----
    def __enter__(self):
        self.open()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()

    # ---- open / close ----
    def open(self):
        self.fd = os.open(self.path, os.O_RDWR | os.O_CLOEXEC)
        # 尝试拿 master；如果失败说明别人（KWin）正持有，立即报错
        try:
            _ioctl(self.fd, DRM_IOCTL_SET_MASTER, C.c_int(0))
            self.is_master = True
        except OSError as e:
            os.close(self.fd)
            self.fd = -1
            raise RuntimeError(
                f"无法对 {self.path} 取得 DRM master。"
                f"通常意味着 KWin 等 compositor 还持有该卡。"
                f"请确认 udev 规则已生效。底层错误: {e}"
            )

    def close(self):
        # 1) 释放所有 dumb buffer
        for b in self._buffers:
            self._destroy_buffer(b)
        self._buffers.clear()

        # 2) 关 CRTC（清屏，避免留下残影；KMS-only 卡断了 master 屏就黑）
        if self.crtc_id and self.fd >= 0:
            try:
                self._disable_crtc()
            except OSError:
                pass

        # 3) drop master + close
        if self.fd >= 0:
            if self.is_master:
                try:
                    _ioctl(self.fd, DRM_IOCTL_DROP_MASTER, C.c_int(0))
                except OSError:
                    pass
            os.close(self.fd)
            self.fd = -1

    # ---- enumerate resources ----
    def _get_resources(self):
        res = drm_mode_card_res()
        _ioctl(self.fd, DRM_IOCTL_MODE_GETRESOURCES, res)
        # 第二次调用前要分配数组
        crtc_ids = (C.c_uint32 * res.count_crtcs)()
        conn_ids = (C.c_uint32 * res.count_connectors)()
        enc_ids  = (C.c_uint32 * res.count_encoders)()
        fb_ids   = (C.c_uint32 * res.count_fbs)()
        res.crtc_id_ptr      = C.cast(crtc_ids, C.c_void_p).value or 0
        res.connector_id_ptr = C.cast(conn_ids, C.c_void_p).value or 0
        res.encoder_id_ptr   = C.cast(enc_ids,  C.c_void_p).value or 0
        res.fb_id_ptr        = C.cast(fb_ids,   C.c_void_p).value or 0
        _ioctl(self.fd, DRM_IOCTL_MODE_GETRESOURCES, res)
        return list(crtc_ids), list(conn_ids), list(enc_ids)

    def _get_connector(self, conn_id: int) -> ConnectorInfo:
        c = drm_mode_get_connector()
        c.connector_id = conn_id
        _ioctl(self.fd, DRM_IOCTL_MODE_GETCONNECTOR, c)
        # 分配数组并再来一次
        modes = (drm_mode_modeinfo * c.count_modes)()
        encs  = (C.c_uint32 * c.count_encoders)()
        props = (C.c_uint32 * c.count_props)()
        pvals = (C.c_uint64 * c.count_props)()
        c.modes_ptr       = C.cast(modes, C.c_void_p).value or 0
        c.encoders_ptr    = C.cast(encs,  C.c_void_p).value or 0
        c.props_ptr       = C.cast(props, C.c_void_p).value or 0
        c.prop_values_ptr = C.cast(pvals, C.c_void_p).value or 0
        _ioctl(self.fd, DRM_IOCTL_MODE_GETCONNECTOR, c)
        return ConnectorInfo(
            id=c.connector_id,
            connection=c.connection,
            encoder_id=c.encoder_id,
            modes=list(modes),
            mm_width=c.mm_width,
            mm_height=c.mm_height,
            possible_encoders=list(encs),
        )

    def _get_encoder(self, enc_id: int) -> EncoderInfo:
        e = drm_mode_get_encoder()
        e.encoder_id = enc_id
        _ioctl(self.fd, DRM_IOCTL_MODE_GETENCODER, e)
        return EncoderInfo(id=e.encoder_id, crtc_id=e.crtc_id, possible_crtcs=e.possible_crtcs)

    # ---- modeset ----
    def modeset(self, prefer_native=True):
        """
        自动选第一个 connected connector，取它的 preferred mode
        (或第一个 mode)，绑到任一可用 CRTC。
        """
        crtcs, conns, _ = self._get_resources()

        # 找第一个 connected
        chosen_conn: Optional[ConnectorInfo] = None
        for cid in conns:
            ci = self._get_connector(cid)
            if ci.connection == DRM_MODE_CONNECTED and len(ci.modes) > 0:
                chosen_conn = ci
                break
        if not chosen_conn:
            raise RuntimeError("没找到 connected connector")
        self.connector = chosen_conn

        # 选 mode：优先 PREFERRED，否则取第一个
        DRM_MODE_TYPE_PREFERRED = 1 << 3
        chosen_mode = chosen_conn.modes[0]
        if prefer_native:
            for m in chosen_conn.modes:
                if m.type & DRM_MODE_TYPE_PREFERRED:
                    chosen_mode = m
                    break
        self.mode = chosen_mode
        self.width = chosen_mode.hdisplay
        self.height = chosen_mode.vdisplay

        # 找 encoder：优先用 connector 的 current encoder_id；
        # 没有时（KWin 未启用过该 connector 的情况）从 possible_encoders 取第一个。
        enc_id = chosen_conn.encoder_id
        if enc_id == 0:
            if not chosen_conn.possible_encoders:
                raise RuntimeError("connector 没有任何可用 encoder")
            enc_id = chosen_conn.possible_encoders[0]
        enc = self._get_encoder(enc_id)
        self.encoder = enc

        crtc_id = 0
        for idx, c_id in enumerate(crtcs):
            if enc.possible_crtcs & (1 << idx):
                crtc_id = c_id
                break
        if not crtc_id:
            raise RuntimeError("找不到兼容的 CRTC")
        self.crtc_id = crtc_id

    # ---- dumb buffer ----
    def create_framebuffer(self, bpp=32, depth=24) -> DumbBuffer:
        """创建 XRGB8888 dumb buffer，已 ADDFB + mmap。"""
        if not self.mode:
            raise RuntimeError("先调用 modeset()")

        cd = drm_mode_create_dumb()
        cd.width = self.width
        cd.height = self.height
        cd.bpp = bpp
        _ioctl(self.fd, DRM_IOCTL_MODE_CREATE_DUMB, cd)

        # ADDFB
        fb = drm_mode_fb_cmd()
        fb.width = self.width
        fb.height = self.height
        fb.pitch = cd.pitch
        fb.bpp = bpp
        fb.depth = depth
        fb.handle = cd.handle
        _ioctl(self.fd, DRM_IOCTL_MODE_ADDFB, fb)

        # MAP_DUMB
        md = drm_mode_map_dumb()
        md.handle = cd.handle
        _ioctl(self.fd, DRM_IOCTL_MODE_MAP_DUMB, md)

        # mmap
        mm = mmap.mmap(self.fd, cd.size, mmap.MAP_SHARED,
                       mmap.PROT_READ | mmap.PROT_WRITE, offset=md.offset)

        buf = DumbBuffer(
            handle=cd.handle, pitch=cd.pitch, size=cd.size,
            fb_id=fb.fb_id, map_offset=md.offset, mmap_obj=mm,
        )
        self._buffers.append(buf)
        return buf

    def _destroy_buffer(self, buf: DumbBuffer):
        if buf.mmap_obj:
            buf.mmap_obj.close()
            buf.mmap_obj = None
        if buf.fb_id:
            fb_id = C.c_uint32(buf.fb_id)
            try:
                _ioctl(self.fd, DRM_IOCTL_MODE_RMFB, fb_id)
            except OSError:
                pass
            buf.fb_id = 0
        if buf.handle:
            dd = drm_mode_destroy_dumb()
            dd.handle = buf.handle
            try:
                _ioctl(self.fd, DRM_IOCTL_MODE_DESTROY_DUMB, dd)
            except OSError:
                pass
            buf.handle = 0

    # ---- modeset commit ----
    def flip_to(self, buf: DumbBuffer):
        """
        推一帧到 CRTC。
        第一次调用做完整 modeset (SETCRTC)；之后用 PAGE_FLIP 切换 fb。

        关键：PAGE_FLIP 会触发 ms912x 内核驱动的 simple_display_pipe
        update 回调，让它把新 fb 内容重新编码到 USB transfer_buffer 推送。
        如果反复用 SETCRTC 同一 mode + 不同 fb，部分驱动会"识别为没变"
        而不更新硬件，导致屏幕停在第一帧。
        """
        if not self.mode or not self.crtc_id:
            raise RuntimeError("先调用 modeset()")

        # 第一次：完整 SETCRTC
        if not getattr(self, "_modeset_done", False):
            crtc = drm_mode_crtc()
            crtc.crtc_id = self.crtc_id
            crtc.fb_id = buf.fb_id
            crtc.x = 0
            crtc.y = 0
            conn_arr = (C.c_uint32 * 1)(self.connector.id)
            crtc.set_connectors_ptr = C.cast(conn_arr, C.c_void_p).value or 0
            crtc.count_connectors = 1
            crtc.mode = self.mode
            crtc.mode_valid = 1
            _ioctl(self.fd, DRM_IOCTL_MODE_SETCRTC, crtc)
            self._modeset_done = True
            return

        # 之后：PAGE_FLIP（非阻塞、async）
        flip = drm_mode_crtc_page_flip()
        flip.crtc_id = self.crtc_id
        flip.fb_id = buf.fb_id
        flip.flags = 0   # 同步 page flip，无 event
        flip.user_data = 0
        try:
            _ioctl(self.fd, DRM_IOCTL_MODE_PAGE_FLIP, flip)
        except OSError as e:
            # PAGE_FLIP 失败（如 -EBUSY = 上次 flip 还没完成），退回 SETCRTC
            # 这是兜底，绝大多数情况 PAGE_FLIP 应该成功
            crtc = drm_mode_crtc()
            crtc.crtc_id = self.crtc_id
            crtc.fb_id = buf.fb_id
            conn_arr = (C.c_uint32 * 1)(self.connector.id)
            crtc.set_connectors_ptr = C.cast(conn_arr, C.c_void_p).value or 0
            crtc.count_connectors = 1
            crtc.mode = self.mode
            crtc.mode_valid = 1
            _ioctl(self.fd, DRM_IOCTL_MODE_SETCRTC, crtc)

    def _disable_crtc(self):
        """关 CRTC（用 fb_id=0, mode_valid=0）。"""
        crtc = drm_mode_crtc()
        crtc.crtc_id = self.crtc_id
        crtc.fb_id = 0
        crtc.count_connectors = 0
        crtc.mode_valid = 0
        _ioctl(self.fd, DRM_IOCTL_MODE_SETCRTC, crtc)
