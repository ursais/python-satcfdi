"""
Facturama Multiemisor PAC adapter.

Facturama seals and stamps from a JSON payload (CSD must already be uploaded).
Pre-signed XML stamp is not supported by their public Multiemisor API.

Docs: https://apisandbox.facturama.mx/guias
"""
from __future__ import annotations

import base64
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any

import requests
from requests.auth import HTTPBasicAuth

from . import (
    PAC,
    Accept,
    CancelReason,
    CancelationAcknowledgment,
    Document,
    Environment,
)
from .. import __version__
from ..cfdi import CFDI
from ..exceptions import DocumentNotFoundError, ResponseError
from ..models import Signer
from ..utils import iterate

_TAX_NAME = {
    "001": "ISR",
    "002": "IVA",
    "003": "IEPS",
}

_SUPPORTED_TIPOS = {"I", "E"}


def _enum_value(val):
    if isinstance(val, Enum):
        return val.value
    return val


def _num(val) -> float | None:
    if val is None:
        return None
    if isinstance(val, Decimal):
        return float(val)
    if isinstance(val, (int, float)):
        return float(val)
    return float(Decimal(str(val)))


def _format_date(fecha) -> str | None:
    if fecha is None:
        return None
    if isinstance(fecha, datetime):
        return fecha.strftime("%Y-%m-%d %H:%M:%S")
    text = str(fecha)
    if "T" in text:
        return text.replace("T", " ")[:19]
    return text


def _tax_entry(tax: dict, *, is_retention: bool) -> dict:
    impuesto = str(_enum_value(tax.get("Impuesto")))
    name = _TAX_NAME.get(impuesto)
    if not name:
        raise NotImplementedError(f"Unsupported tax code: {impuesto}")

    tipo_factor = str(_enum_value(tax.get("TipoFactor") or ""))
    entry = {
        "Name": name,
        "IsRetention": is_retention,
        "Base": _num(tax.get("Base")),
        "Total": _num(tax.get("Importe")) or 0.0,
    }
    if tipo_factor.lower() == "exento":
        entry["Rate"] = 0.0
        entry["IsQuota"] = False
    else:
        rate = tax.get("TasaOCuota")
        entry["Rate"] = _num(rate) if rate is not None else 0.0
        entry["IsQuota"] = tipo_factor.lower() == "cuota"
    return entry


def cfdi_to_facturama_payload(cfdi: CFDI) -> dict:
    """Map a satcfdi CFDI (typically unsigned) to Facturama Multiemisor JSON."""
    if not isinstance(cfdi, CFDI):
        raise TypeError("cfdi must be a CFDI object")

    tipo = str(_enum_value(cfdi.get("TipoDeComprobante")))
    if tipo not in _SUPPORTED_TIPOS:
        raise NotImplementedError(
            f"Facturama Multiemisor adapter currently supports TipoDeComprobante "
            f"I/E only, got {tipo!r}"
        )

    complemento = cfdi.get("Complemento") or {}
    extra_keys = {k for k in complemento if k != "TimbreFiscalDigital"}
    if extra_keys:
        raise NotImplementedError(
            f"Unsupported Complemento nodes for Facturama mapper: {sorted(extra_keys)}"
        )

    emisor = cfdi["Emisor"]
    receptor = cfdi["Receptor"]

    payload: dict[str, Any] = {
        "CfdiType": tipo,
        "ExpeditionPlace": str(cfdi["LugarExpedicion"]),
        "Issuer": {
            "Rfc": emisor["Rfc"],
            "Name": emisor["Nombre"],
            "FiscalRegime": str(_enum_value(emisor["RegimenFiscal"])),
        },
        "Receiver": {
            "Rfc": receptor["Rfc"],
            "Name": receptor["Nombre"],
            "CfdiUse": str(_enum_value(receptor["UsoCFDI"])),
            "FiscalRegime": str(_enum_value(receptor["RegimenFiscalReceptor"])),
            "TaxZipCode": str(receptor["DomicilioFiscalReceptor"]),
        },
        "Items": [],
    }

    if fecha := _format_date(cfdi.get("Fecha")):
        payload["Date"] = fecha
    if serie := cfdi.get("Serie"):
        payload["Serie"] = str(serie)
    if folio := cfdi.get("Folio"):
        payload["Folio"] = str(folio)
    if forma := cfdi.get("FormaPago"):
        payload["PaymentForm"] = str(_enum_value(forma))
    if metodo := cfdi.get("MetodoPago"):
        payload["PaymentMethod"] = str(_enum_value(metodo))
    if moneda := cfdi.get("Moneda"):
        payload["Currency"] = str(_enum_value(moneda))
    if tipo_cambio := cfdi.get("TipoCambio"):
        payload["ExchangeRate"] = _num(tipo_cambio)
    if descuento := cfdi.get("Descuento"):
        payload["Discount"] = _num(descuento)
    if condiciones := cfdi.get("CondicionesDePago"):
        payload["PaymentConditions"] = str(condiciones)
    if exportacion := cfdi.get("Exportacion"):
        payload["Exportation"] = str(_enum_value(exportacion))
    if confirmacion := cfdi.get("Confirmacion"):
        payload["Confirmation"] = str(confirmacion)

    if info_global := cfdi.get("InformacionGlobal"):
        payload["GlobalInformation"] = {
            "Periodicity": str(_enum_value(info_global["Periodicidad"])),
            "Months": str(_enum_value(info_global["Meses"])),
            "Year": str(info_global["Año"]),
        }

    if relacionados := cfdi.get("CfdiRelacionados"):
        # satcfdi may store one node or a list
        rel = next(iterate(relacionados))
        payload["Relations"] = {
            "Type": str(_enum_value(rel["TipoRelacion"])),
            "Cfdis": [
                {"Uuid": str(uuid_node["UUID"])}
                for uuid_node in iterate(rel.get("CfdiRelacionado"))
            ],
        }

    for concepto in iterate(cfdi["Conceptos"]):
        item: dict[str, Any] = {
            "ProductCode": str(concepto["ClaveProdServ"]),
            "Description": str(concepto["Descripcion"]),
            "UnitCode": str(concepto["ClaveUnidad"]),
            "Quantity": _num(concepto["Cantidad"]),
            "UnitPrice": _num(concepto["ValorUnitario"]),
            "Subtotal": _num(concepto["Importe"]),
            "TaxObject": str(_enum_value(concepto.get("ObjetoImp") or "01")),
        }
        if no_id := concepto.get("NoIdentificacion"):
            item["IdentificationNumber"] = str(no_id)
        if unidad := concepto.get("Unidad"):
            item["Unit"] = str(unidad)
        if desc := concepto.get("Descuento"):
            item["Discount"] = _num(desc)

        taxes = []
        impuestos = concepto.get("Impuestos") or {}
        for traslado in iterate(impuestos.get("Traslados")):
            taxes.append(_tax_entry(traslado, is_retention=False))
        for retencion in iterate(impuestos.get("Retenciones")):
            taxes.append(_tax_entry(retencion, is_retention=True))
        if taxes:
            item["Taxes"] = taxes

        # Facturama expects line Total (with taxes, after discount)
        line_total = _num(concepto["Importe"]) or 0.0
        if desc := item.get("Discount"):
            line_total -= desc
        for tax in taxes:
            if tax["IsRetention"]:
                line_total -= tax["Total"] or 0.0
            else:
                line_total += tax["Total"] or 0.0
        item["Total"] = line_total

        payload["Items"].append(item)

    return payload


class Facturama(PAC):
    """
    Facturama Multiemisor API adapter.

    Facturama is a billing platform that seals+stamps via Multiemisor; CSD files
    must be uploaded first (see :meth:`upload_csd`).

    Documentation: https://apisandbox.facturama.mx/guias/api-multi/proceso-facturacion
    """

    # Facturama aggregates multiple PACs; no single PAC RFC applies.
    RFC = None

    def __init__(self, username: str, password: str, environment=Environment.PRODUCTION):
        super().__init__(environment=environment)
        self.auth = HTTPBasicAuth(username, password)

    @property
    def host(self) -> str:
        match self.environment:
            case Environment.PRODUCTION:
                return "https://api.facturama.mx"
            case Environment.TEST:
                return "https://apisandbox.facturama.mx"
            case _:
                raise NotImplementedError("Environment not supported")

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict | None = None,
        params: dict | None = None,
    ):
        r = requests.request(
            method=method,
            url=f"{self.host}/{path.lstrip('/')}",
            headers={
                "User-Agent": __version__.__user_agent__,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            auth=self.auth,
            json=json,
            params=params,
        )
        if r.ok:
            if not r.content:
                return None
            content_type = (r.headers.get("Content-Type") or "").lower()
            if "application/json" in content_type:
                return r.json()
            return r.content
        raise ResponseError(r)

    def _download_file(self, document_id: str, fmt: str) -> bytes:
        # Multiemisor downloads go through /api/Cfdi/{format}/issuedLite/{id}
        res = self._request(
            "get",
            f"api/Cfdi/{fmt}/issuedLite/{document_id}",
        )
        if isinstance(res, dict) and "Content" in res:
            return base64.urlsafe_b64decode(res["Content"].encode("utf-8"))
        if isinstance(res, (bytes, bytearray)):
            return bytes(res)
        raise ResponseError(res)

    def upload_csd(
        self,
        rfc: str,
        certificate: bytes,
        key: bytes,
        password: str | bytes,
    ) -> dict:
        """Upload issuer CSD required before Multiemisor issue()."""
        if isinstance(password, bytes):
            password = password.decode()
        return self._request(
            "post",
            "api-lite/csds",
            json={
                "Rfc": rfc.upper(),
                "Certificate": base64.b64encode(certificate).decode("ascii"),
                "PrivateKey": base64.b64encode(key).decode("ascii"),
                "PrivateKeyPassword": password,
            },
        )

    def delete_csd(self, rfc: str) -> None:
        self._request("delete", f"api-lite/csds/{rfc.upper()}")

    def get_csd(self, rfc: str) -> dict:
        return self._request("get", f"api-lite/csds/{rfc.upper()}")

    def issue(self, cfdi: CFDI, accept: Accept = Accept.XML) -> Document:
        """
        Seal and stamp via Facturama Multiemisor.

        The local CFDI does not need to be signed; Facturama seals with the
        uploaded CSD for the issuer RFC.
        """
        payload = cfdi_to_facturama_payload(cfdi)
        created = self._request("post", "api-lite/3/cfdis", json=payload)
        document_id = created["Id"]

        xml = None
        pdf = None
        if accept & Accept.XML:
            xml = self._download_file(document_id, "xml")
        if accept & Accept.PDF:
            pdf = self._download_file(document_id, "pdf")

        return Document(document_id=document_id, xml=xml, pdf=pdf)

    def stamp(self, cfdi: CFDI, accept: Accept = Accept.XML) -> Document:
        raise NotImplementedError(
            "Facturama Multiemisor does not stamp pre-signed XML; use issue() "
            "(Facturama seals and stamps from the mapped JSON payload)."
        )

    def recover(self, document_id: str, accept: Accept = Accept.XML) -> Document:
        xml = self._download_file(document_id, "xml") if accept & Accept.XML else None
        pdf = self._download_file(document_id, "pdf") if accept & Accept.PDF else None
        return Document(document_id=document_id, xml=xml, pdf=pdf)

    def cancel(
        self,
        cfdi: CFDI,
        reason: CancelReason,
        substitution_id: str = None,
        signer: Signer = None,
        document_id: str = None,
    ) -> CancelationAcknowledgment:
        """
        Cancel a Multiemisor CFDI.

        Prefer passing ``document_id`` (Facturama Id returned by :meth:`issue`).
        If omitted, looks up the Id by UUID from the CFDI TimbreFiscalDigital.
        """
        del signer  # Facturama cancels with account credentials, not local FIEL
        facturama_id = document_id or self._find_id_by_uuid(
            cfdi["Complemento"]["TimbreFiscalDigital"]["UUID"]
        )
        params = {"motive": reason.value}
        if substitution_id:
            params["uuidReplacement"] = substitution_id

        res = self._request(
            "delete",
            f"api-lite/cfdis/{facturama_id}",
            params=params,
        )
        status = None
        acuse = None
        if isinstance(res, dict):
            status = res.get("Status") or res.get("Message") or res
            if content := res.get("AcuseXmlBase64") or res.get("Acuse"):
                if isinstance(content, str):
                    try:
                        acuse = base64.b64decode(content)
                    except Exception:
                        acuse = content.encode() if content else None
        return CancelationAcknowledgment(code=status or "cancelled", acuse=acuse)

    def _find_id_by_uuid(self, uuid: str) -> str:
        results = self._request(
            "get",
            "api-lite/cfdis",
            params={"type": "issuedLite", "keyword": uuid, "status": "all"},
        )
        items = results if isinstance(results, list) else (results or {}).get("data") or []
        for item in items:
            if str(item.get("Uuid") or item.get("UUID") or "").upper() == str(uuid).upper():
                return item["Id"]
            if str(item.get("Id")) == str(uuid):
                return item["Id"]
        raise DocumentNotFoundError(
            f"Facturama document not found for UUID {uuid}"
        )
