import json
from datetime import datetime
from decimal import Decimal
from unittest import mock

from requests.auth import HTTPBasicAuth

from satcfdi.create.cfd import cfdi40
from satcfdi.create.cfd.catalogos import Impuesto, TipoFactor
from satcfdi.pacs import Accept, CancelReason, Environment
from satcfdi.pacs.facturama import Facturama, cfdi_to_facturama_payload
from .utils import get_signer, verify_result


def _sample_invoice():
    signer = get_signer("xiqb891116qe4")
    invoice = cfdi40.Comprobante(
        emisor=cfdi40.Emisor(
            rfc=signer.rfc,
            nombre=signer.legal_name,
            regimen_fiscal="601",
        ),
        lugar_expedicion="27200",
        fecha=datetime.fromisoformat("2022-09-28T22:40:38"),
        receptor=cfdi40.Receptor(
            rfc="URE180429TM6",
            nombre="UNIVERSIDAD ROBOTICA ESPAÑOLA",
            uso_cfdi="G03",
            domicilio_fiscal_receptor="65000",
            regimen_fiscal_receptor="601",
        ),
        metodo_pago="PPD",
        forma_pago="99",
        serie="T",
        folio="1000",
        conceptos=cfdi40.Concepto(
            clave_prod_serv="10101702",
            cantidad=Decimal("1.00"),
            clave_unidad="E48",
            descripcion="SERVICIOS DE RENTA",
            valor_unitario=Decimal("100.00"),
            impuestos=cfdi40.Impuestos(
                traslados=cfdi40.Traslado(
                    impuesto=Impuesto.IVA,
                    tipo_factor=TipoFactor.TASA,
                    tasa_o_cuota=Decimal("0.160000"),
                ),
                retenciones=[
                    cfdi40.Traslado(
                        impuesto=Impuesto.ISR,
                        tipo_factor=TipoFactor.TASA,
                        tasa_o_cuota=Decimal("0.100000"),
                    ),
                ],
            ),
        ),
    )
    return invoice


def test_cfdi_to_facturama_payload():
    payload = cfdi_to_facturama_payload(_sample_invoice())
    verify = verify_result(
        data=json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True),
        filename="test_payload.json",
    )
    assert verify
    assert payload["CfdiType"] == "I"
    assert payload["Issuer"]["Rfc"] == "XIQB891116QE4"
    assert payload["Items"][0]["Taxes"]


def test_facturama_issue():
    pac = Facturama(
        username="user",
        password="password",
        environment=Environment.TEST,
    )
    invoice = _sample_invoice()

    with mock.patch("requests.request") as mk:
        create_response = mock.Mock()
        create_response.ok = True
        create_response.content = b'{"Id":"abc123","Uuid":"6d7434a6-e3f2-47ad-9e4c-08849946afa0"}'
        create_response.headers = {"Content-Type": "application/json"}
        create_response.json = mock.Mock(
            return_value={
                "Id": "abc123",
                "Uuid": "6d7434a6-e3f2-47ad-9e4c-08849946afa0",
            }
        )

        xml_response = mock.Mock()
        xml_response.ok = True
        xml_response.content = b'{"Content":"PD94bWwgdmVyc2lvbj0iMS4wIj8+PGNmZGk+dGVzdDwvY2ZkaT4="}'
        xml_response.headers = {"Content-Type": "application/json"}
        xml_response.json = mock.Mock(
            return_value={
                # base64url of: <?xml version="1.0"?><cfdi>test</cfdi>
                "Content": "PD94bWwgdmVyc2lvbj0iMS4wIj8+PGNmZGk+dGVzdDwvY2ZkaT4=",
            }
        )
        mk.side_effect = [create_response, xml_response]

        res = pac.issue(cfdi=invoice, accept=Accept.XML)

        assert res.document_id == "abc123"
        assert res.xml.startswith(b"<?xml")
        assert mk.call_count == 2
        assert mk.call_args_list[0].kwargs["auth"] == HTTPBasicAuth("user", "password")
        assert mk.call_args_list[0].kwargs["url"].endswith("api-lite/3/cfdis")

        # Normalize volatile headers for golden file
        for call in mk.call_args_list:
            call.kwargs["headers"]["User-Agent"] = "this is a test"
            call.kwargs["auth"] = "Basic abc"

        args = json.dumps(
            [c.kwargs for c in mk.call_args_list],
            indent=2,
            default=str,
            ensure_ascii=False,
        )
        verify = verify_result(data=args, filename="test_issue.json")
        assert verify


def test_facturama_stamp_not_supported():
    pac = Facturama("user", "password", Environment.TEST)
    try:
        pac.stamp(_sample_invoice())
        assert False, "expected NotImplementedError"
    except NotImplementedError as exc:
        assert "issue()" in str(exc)


def test_facturama_recover_and_cancel():
    pac = Facturama("user", "password", Environment.TEST)

    with mock.patch("requests.request") as mk:
        xml_response = mock.Mock()
        xml_response.ok = True
        xml_response.content = b'{"Content":"PD94bWwgdmVyc2lvbj0iMS4wIj8+PGNmZGk+dGVzdDwvY2ZkaT4="}'
        xml_response.headers = {"Content-Type": "application/json"}
        xml_response.json = mock.Mock(
            return_value={"Content": "PD94bWwgdmVyc2lvbj0iMS4wIj8+PGNmZGk+dGVzdDwvY2ZkaT4="}
        )

        cancel_response = mock.Mock()
        cancel_response.ok = True
        cancel_response.content = b'{"Status":"canceled"}'
        cancel_response.headers = {"Content-Type": "application/json"}
        cancel_response.json = mock.Mock(return_value={"Status": "canceled"})

        mk.side_effect = [xml_response, cancel_response]

        recovered = pac.recover("abc123", accept=Accept.XML)
        assert recovered.document_id == "abc123"
        assert recovered.xml.startswith(b"<?xml")

        from satcfdi.cfdi import CFDI

        cfdi = CFDI(
            {
                "Emisor": {"Rfc": "XIQB891116QE4"},
                "Complemento": {
                    "TimbreFiscalDigital": {
                        "UUID": "6D7434A6-E3F2-47AD-9E4C-08849946AFA0"
                    }
                },
            }
        )
        ack = pac.cancel(
            cfdi=cfdi,
            reason=CancelReason.COMPROBANTE_EMITIDO_CON_ERRORES_SIN_RELACION,
            document_id="abc123",
        )
        assert ack.code == "canceled"
        assert mk.call_args_list[1].kwargs["params"]["motive"] == "02"


def test_facturama_upload_csd():
    pac = Facturama("user", "password", Environment.TEST)
    with mock.patch("requests.request") as mk:
        mk.return_value.ok = True
        mk.return_value.content = b'{"Rfc":"XIQB891116QE4"}'
        mk.return_value.headers = {"Content-Type": "application/json"}
        mk.return_value.json = mock.Mock(return_value={"Rfc": "XIQB891116QE4"})

        pac.upload_csd(
            rfc="xiqb891116qe4",
            certificate=b"CER",
            key=b"KEY",
            password="secret",
        )
        assert mk.called
        body = mk.call_args.kwargs["json"]
        assert body["Rfc"] == "XIQB891116QE4"
        assert body["Certificate"]
        assert body["PrivateKey"]
        assert body["PrivateKeyPassword"] == "secret"
        assert mk.call_args.kwargs["url"].endswith("api-lite/csds")
