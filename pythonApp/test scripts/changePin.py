from __future__ import annotations

import argparse
import ctypes
import logging
import os
import platform
from ctypes import c_char_p, c_int, c_void_p, create_string_buffer

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


# ----------------------------
# PullSDK loader / bindings
# ----------------------------
def load_plcommpro(dll_path: str | None = None):
    if platform.system() != "Windows":
        raise EnvironmentError("PullSDK plcommpro.dll fonctionne uniquement sur Windows.")

    if dll_path is None:
        dll_path = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "System32", "plcommpro.dll")

    if not os.path.exists(dll_path):
        raise FileNotFoundError(f"plcommpro.dll introuvable: {dll_path}")

    pl = ctypes.CDLL(dll_path)

    pl.Connect.argtypes = [c_char_p]
    pl.Connect.restype = c_void_p

    pl.Disconnect.argtypes = [c_void_p]

    pl.GetDeviceData.argtypes = [c_void_p, c_char_p, c_int, c_char_p, c_char_p, c_char_p, c_char_p]
    pl.GetDeviceData.restype = c_int

    pl.SetDeviceData.argtypes = [c_void_p, c_char_p, c_char_p, c_char_p]
    pl.SetDeviceData.restype = c_int

    pl.DeleteDeviceData.argtypes = [c_void_p, c_char_p, c_char_p, c_char_p]
    pl.DeleteDeviceData.restype = c_int

    try:
        pl.PullLastError.argtypes = []
        pl.PullLastError.restype = c_int
    except Exception:
        pass

    return pl


def pull_last_error(pl):
    try:
        return pl.PullLastError()
    except Exception:
        return None


def connect(pl, ip: str, port: int = 4370, timeout_ms: int = 4000, passwd: str = ""):
    params = f"protocol=TCP,ipaddress={ip},port={port},timeout={timeout_ms},passwd={passwd}".encode("utf-8")
    h = pl.Connect(params)
    if not h:
        raise ConnectionError(f"Connexion échouée: {ip}:{port} (PullLastError={pull_last_error(pl)})")
    logging.info("✅ Connecté à %s:%s (handle=%s)", ip, port, h)
    return h


def disconnect(pl, handle):
    try:
        if handle:
            pl.Disconnect(handle)
    except Exception:
        pass


# ----------------------------
# PullSDK helpers
# ----------------------------
def get_device_data_safe(pl, handle, table: str, field_names: str = "*", filter_str: str = "", options: str = "",
                         buf_size: int = 10 * 1024 * 1024) -> tuple[int, str]:
    """
    PullSDK: ret >= 0 = succès (nb records), ret < 0 = erreur. :contentReference[oaicite:3]{index=3}
    """
    buf = create_string_buffer(buf_size)
    ret = pl.GetDeviceData(
        handle,
        buf,
        buf_size,
        table.encode("utf-8"),
        field_names.encode("utf-8"),
        filter_str.encode("utf-8"),
        options.encode("utf-8"),
    )
    text = buf.value.decode("utf-8", errors="ignore").strip()
    return ret, text


def set_device_data(pl, handle, table: str, data: str) -> int:
    return pl.SetDeviceData(handle, table.encode("utf-8"), data.encode("utf-8"), b"")


def delete_device_data(pl, handle, table: str, cond: str) -> int:
    return pl.DeleteDeviceData(handle, table.encode("utf-8"), cond.encode("utf-8"), None)


def parse_records(raw: str):
    if not raw:
        return []
    lines = raw.replace("\r\n", "\n").split("\n")
    lines = [l.strip() for l in lines if l.strip()]
    if not lines:
        return []

    # CSV header
    if ("," in lines[0]) and ("=" not in lines[0]):
        header = [h.strip() for h in lines[0].split(",")]
        out = []
        for line in lines[1:]:
            cols = [c.strip() for c in line.split(",")]
            rec = {header[i]: (cols[i] if i < len(cols) else "") for i in range(len(header))}
            out.append(rec)
        return out

    # key=value\tkey=value
    out = []
    for line in lines:
        rec = {}
        for part in line.split("\t"):
            if "=" in part:
                k, v = part.split("=", 1)
                rec[k.strip()] = v.strip()
        if rec:
            out.append(rec)
    return out


def get_records_by_pin(pl, handle, table: str, pin: str):
    """
    IMPORTANT (basé sur tes tests):
    - pour templatev10 en lecture: utiliser uniquement Pin=... (PIN=... peut renvoyer -101)
    """
    pin = str(pin)
    filters = [f"Pin={pin}\t", f"Pin={pin}"]

    for f in filters:
        ret, raw = get_device_data_safe(pl, handle, table, field_names="*", filter_str=f)
        if ret >= 0:
            return parse_records(raw)
        if ret == -101:
            # filtre non accepté / structure non supportée :contentReference[oaicite:4]{index=4}
            continue
        raise RuntimeError(f"GetDeviceData(table={table}, filter={f!r}) a échoué (ret={ret})")

    return []


# ----------------------------
# Delete routines
# ----------------------------
def must_delete_ok(ret: int, context: str):
    if ret != 0:
        raise RuntimeError(f"{context} a échoué (ret={ret})")


def delete_all_userauthorize_for_pin(pl, handle, pin: str):
    pin = str(pin)
    ret = delete_device_data(pl, handle, "userauthorize", f"Pin={pin}")
    # userauthorize delete renvoie souvent 0 même si rien; si non 0 on log soft
    if ret != 0:
        logging.warning("⚠️ Delete userauthorize Pin=%s ret=%s (PullLastError=%s)", pin, ret, pull_last_error(pl))


def delete_all_templates_for_pin(pl, handle, pin: str):
    pin = str(pin)
    recs = get_records_by_pin(pl, handle, "templatev10", pin)
    if not recs:
        return

    deleted = 0
    for r in recs:
        fid = r.get("FingerID") or r.get("FingerId")
        if not fid:
            continue

        # conditions compatibles (Pin=...)
        candidates = [
            f"Pin={pin}\tFingerID={fid}",
            f"Pin={pin}\tFingerID={fid}\tValid=1\tResverd=\tEndTag=",
        ]

        ok = False
        for cond in candidates:
            ret = delete_device_data(pl, handle, "templatev10", cond)
            if ret == 0:
                ok = True
                deleted += 1
                break

        if not ok:
            logging.warning("❌ Impossible de supprimer templatev10 Pin=%s FingerID=%s (PullLastError=%s)",
                            pin, fid, pull_last_error(pl))

    logging.info("🗑️ templatev10 supprimés pour Pin=%s : %s", pin, deleted)


def delete_user_for_pin(pl, handle, pin: str):
    pin = str(pin)
    ret = delete_device_data(pl, handle, "user", f"Pin={pin}")
    if ret != 0:
        # certains firmwares refusent la suppression user ; on log soft
        logging.warning("⚠️ Delete user Pin=%s ret=%s (PullLastError=%s)", pin, ret, pull_last_error(pl))


# ----------------------------
# Write routines
# ----------------------------
def must_set_ok(ret: int, context: str, pl):
    if ret != 0:
        raise RuntimeError(f"{context} a échoué (ret={ret}, PullLastError={pull_last_error(pl)})")


def write_user(pl, handle, src_user: dict, dst_pin: str):
    dst_pin = str(dst_pin)
    rec = dict(src_user)
    rec["Pin"] = dst_pin
    if "UID" in rec:
        rec["UID"] = dst_pin

    data = "\t".join([f"{k}={v}" for k, v in rec.items()])
    must_set_ok(set_device_data(pl, handle, "user", data), "SetDeviceData(user)", pl)


def write_authorizations(pl, handle, auth_records: list[dict], dst_pin: str):
    dst_pin = str(dst_pin)
    if not auth_records:
        return

    # multi-lignes OK généralement
    lines = []
    for r in auth_records:
        rr = dict(r)
        rr["Pin"] = dst_pin
        lines.append("\t".join([f"{k}={v}" for k, v in rr.items()]))

    data = "\r\n".join(lines)
    must_set_ok(set_device_data(pl, handle, "userauthorize", data), "SetDeviceData(userauthorize)", pl)


def normalize_template(s: str) -> str:
    # supprime tout whitespace (CR/LF/spaces) pour éviter un template “cassé”
    return "".join((s or "").split())


def set_template_with_fallbacks(pl, handle, dst_pin: str, fid: str, valid: str, template: str):
    """
    Très robuste: essaie plusieurs formats acceptés par différents firmwares.
    Quand ret=-101, c'est typiquement un champ non reconnu / mauvaise structure :contentReference[oaicite:5]{index=5}
    """
    dst_pin = str(dst_pin)
    fid = str(fid)
    valid = str(valid or "1")
    template = normalize_template(template)

    # IMPORTANT: doc templatev10 champs attendus: Size, UID, PIN, FingerID, Valid, Template, Resverd, EndTag :contentReference[oaicite:6]{index=6}
    # Mais en pratique certains firmwares veulent Pin au lieu de PIN, ou n’aiment pas Resverd/EndTag, ou n’aiment pas UID.
    candidates = [
        # A) "doc strict"
        f"Size={len(template)}\tUID={dst_pin}\tPIN={dst_pin}\tFingerID={fid}\tValid={valid}\tTemplate={template}\tResverd=\tEndTag=",
        # B) Pin au lieu de PIN
        f"Size={len(template)}\tUID={dst_pin}\tPin={dst_pin}\tFingerID={fid}\tValid={valid}\tTemplate={template}\tResverd=\tEndTag=",
        # C) sans Resverd/EndTag
        f"Size={len(template)}\tUID={dst_pin}\tPIN={dst_pin}\tFingerID={fid}\tValid={valid}\tTemplate={template}",
        f"Size={len(template)}\tUID={dst_pin}\tPin={dst_pin}\tFingerID={fid}\tValid={valid}\tTemplate={template}",
        # D) sans UID
        f"Size={len(template)}\tPIN={dst_pin}\tFingerID={fid}\tValid={valid}\tTemplate={template}\tResverd=\tEndTag=",
        f"Size={len(template)}\tPin={dst_pin}\tFingerID={fid}\tValid={valid}\tTemplate={template}\tResverd=\tEndTag=",
        # E) minimal (dernier recours)
        f"PIN={dst_pin}\tFingerID={fid}\tValid={valid}\tTemplate={template}",
        f"Pin={dst_pin}\tFingerID={fid}\tValid={valid}\tTemplate={template}",
    ]

    last_ret = None
    for idx, data in enumerate(candidates, start=1):
        ret = set_device_data(pl, handle, "templatev10", data)
        if ret == 0:
            return idx  # format utilisé
        last_ret = ret

        # Si ce n'est pas -101, c'est probablement un autre problème (busy, etc.)
        # Mais on tente quand même le prochain format une fois.
        continue

    raise RuntimeError(
        f"SetDeviceData(templatev10) a échoué après {len(candidates)} formats "
        f"(last_ret={last_ret}, PullLastError={pull_last_error(pl)})"
    )


def write_templates_one_by_one(pl, handle, tpl_records: list[dict], dst_pin: str):
    dst_pin = str(dst_pin)
    if not tpl_records:
        return

    ok = 0
    for i, r in enumerate(tpl_records, start=1):
        template = r.get("Template") or ""
        fid = r.get("FingerID") or r.get("FingerId") or ""
        valid = r.get("Valid") or "1"

        if not fid:
            logging.warning("⚠️ template sans FingerID ignoré (index=%s)", i)
            continue

        used = set_template_with_fallbacks(pl, handle, dst_pin, fid, valid, template)
        ok += 1
        logging.info("   - template %s/%s écrit (FingerID=%s, format=%s)", i, len(tpl_records), fid, used)

    logging.info("✅ templates écrits sur Pin=%s : %s/%s", dst_pin, ok, len(tpl_records))


def is_empty(pl, handle, table: str, pin: str) -> bool:
    return len(get_records_by_pin(pl, handle, table, pin)) == 0


# ----------------------------
# Replace PIN total
# ----------------------------
def replace_pin_total(pl, handle, src_pin: str, dst_pin: str, clean_dst: bool = True):
    src_pin = str(src_pin)
    dst_pin = str(dst_pin)

    src_users = get_records_by_pin(pl, handle, "user", src_pin)
    if not src_users:
        raise ValueError(f"Aucun user trouvé pour Pin source={src_pin}")
    src_user = src_users[0]

    src_templates = get_records_by_pin(pl, handle, "templatev10", src_pin)
    src_auth = get_records_by_pin(pl, handle, "userauthorize", src_pin)

    logging.info("📌 Source Pin=%s -> user=1, templates=%s, authorizations=%s",
                 src_pin, len(src_templates), len(src_auth))

    # 1) Nettoyage destination
    if clean_dst:
        logging.info("🧹 Nettoyage destination Pin=%s ...", dst_pin)
        try:
            delete_all_userauthorize_for_pin(pl, handle, dst_pin)
        except Exception as e:
            logging.warning("Nettoyage userauthorize dst: %s", e)

        try:
            delete_all_templates_for_pin(pl, handle, dst_pin)
        except Exception as e:
            logging.warning("Nettoyage templatev10 dst: %s", e)

        try:
            delete_user_for_pin(pl, handle, dst_pin)
        except Exception as e:
            logging.warning("Nettoyage user dst: %s", e)

    # 2) Ecriture destination
    logging.info("✍️ Écriture user vers Pin=%s ...", dst_pin)
    write_user(pl, handle, src_user, dst_pin)

    logging.info("✍️ Écriture templates (%s) vers Pin=%s ...", len(src_templates), dst_pin)
    write_templates_one_by_one(pl, handle, src_templates, dst_pin)

    logging.info("✍️ Écriture authorizations (%s) vers Pin=%s ...", len(src_auth), dst_pin)
    write_authorizations(pl, handle, src_auth, dst_pin)

    # 3) Suppression totale source
    logging.info("🗑️ Suppression TOTALE de la source Pin=%s ...", src_pin)
    delete_all_userauthorize_for_pin(pl, handle, src_pin)
    delete_all_templates_for_pin(pl, handle, src_pin)
    delete_user_for_pin(pl, handle, src_pin)

    # 4) Vérifs
    logging.info("🔎 Vérification: source vide ? templatev10=%s userauthorize=%s user=%s",
                 is_empty(pl, handle, "templatev10", src_pin),
                 is_empty(pl, handle, "userauthorize", src_pin),
                 is_empty(pl, handle, "user", src_pin))

    logging.info("🔎 Destination: user=%s templates=%s authorizations=%s",
                 len(get_records_by_pin(pl, handle, "user", dst_pin)),
                 len(get_records_by_pin(pl, handle, "templatev10", dst_pin)),
                 len(get_records_by_pin(pl, handle, "userauthorize", dst_pin)))


# ----------------------------
# CLI
# ----------------------------
def main():
    ap = argparse.ArgumentParser(description="Remplacement TOTAL de PIN sur C3 (PullSDK) - version robuste")
    ap.add_argument("--ip", required=True)
    ap.add_argument("--port", type=int, default=4370)
    ap.add_argument("--src", required=True)
    ap.add_argument("--dst", required=True)
    ap.add_argument("--timeout", type=int, default=4000)
    ap.add_argument("--passwd", default="")
    ap.add_argument("--dll", default=None)
    ap.add_argument("--no-clean-dst", action="store_true")
    args = ap.parse_args()

    pl = load_plcommpro(args.dll)
    h = None
    try:
        h = connect(pl, args.ip, args.port, args.timeout, args.passwd)
        replace_pin_total(pl, h, args.src, args.dst, clean_dst=not args.no_clean_dst)
        logging.info("🎉 Terminé.")
    finally:
        disconnect(pl, h)


if __name__ == "__main__":
    main()
