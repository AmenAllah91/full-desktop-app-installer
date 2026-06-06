from flask import Flask, request, Response
import hashlib
import time
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

app = Flask(__name__)

USER_PIN = "1"
USER_NAME = "Ahmed"
COMMAND_SENT = False


@app.route("/iclock/cdata", methods=["GET", "POST"])
def cdata():
    sn = request.args.get("SN", "?")
    if request.method == "GET":
        logger.info("Appareil connecté: %s", sn)
        session = hashlib.md5(f"{sn}{time.time()}".encode()).hexdigest().upper()
        config = (
            f"GET OPTION FROM: {sn}\r\n"
            f"Stamp=0\r\nOpStamp=0\r\nPhotoStamp=0\r\n"
            f"ErrorDelay=60\r\nDelay=2\r\nRequestDelay=2\r\n"
            f"TransInterval=1\r\nTransFlag=1111000000\r\n"
            f"Realtime=1\r\nEncrypt=0\r\n"
            f"ServerVer=3.4.1\r\nPushProtVer=3.1.2\r\n"
            f"SessionID={session}\r\nTimeoutSec=30"
        )
        return Response(config, status=200, content_type="text/plain")
    else:
        table = request.args.get("table", "")
        body = request.data.decode("utf-8", errors="replace")
        logger.info("POST cdata table=%s: %s", table, body[:200])
        return Response("OK", status=200, content_type="text/plain")


@app.route("/iclock/registry", methods=["GET", "POST"])
def registry():
    sn = request.args.get("SN", "?")
    body = request.data.decode("utf-8", errors="replace")
    logger.info("Registry: SN=%s", sn)
    if body:
        logger.info("   %s", body[:300])
    return Response(f"RegistryCode=\t{sn}", status=200, content_type="text/plain")


@app.route("/iclock/getrequest", methods=["GET"])
def getrequest():
    global COMMAND_SENT
    sn = request.args.get("SN", "?")

    if not COMMAND_SENT:
        COMMAND_SENT = True
        cmd = f"C:1:DATA UPDATE user\tPin={USER_PIN}\tName={USER_NAME}\tPri=0\tPasswd=\tCardNo=\tGrp=1\tTZ=0000000100000000\tVerify=-1\tViceCard="
        logger.info("ENVOI COMMANDE: Ajout user Pin=%s Name=%s", USER_PIN, USER_NAME)
        logger.info("CMD: %s", cmd)
        return Response(cmd, status=200, content_type="text/plain")

    return Response("OK", status=200, content_type="text/plain")


@app.route("/iclock/devicecmd", methods=["POST"])
def devicecmd():
    sn = request.args.get("SN", "?")
    body = request.data.decode("utf-8", errors="replace")
    logger.info("RÉSULTAT COMMANDE: %s", body)
    if "Return=0" in body:
        logger.info("SUCCÈS! L'utilisateur a été ajouté!")
    else:
        logger.error("ÉCHEC de la commande")
    return Response("OK", status=200, content_type="text/plain")


@app.route("/iclock/querydata", methods=["POST"])
def querydata():
    body = request.data.decode("utf-8", errors="replace")
    logger.info("QueryData: %s", body[:300])
    return Response("OK", status=200, content_type="text/plain")


@app.route("/iclock/fdata", methods=["POST"])
def fdata():
    return Response("OK", status=200, content_type="text/plain")


@app.route("/iclock/push", methods=["POST"])
def push():
    return Response("OK", status=200, content_type="text/plain")


if __name__ == "__main__":
    logger.info("=" * 50)
    logger.info("  Ajout user: Pin=%s Name=%s", USER_PIN, USER_NAME)
    logger.info("  En attente de l'appareil sur port 8088...")
    logger.info("=" * 50)
    app.run(host="0.0.0.0", port=8088, debug=False)
