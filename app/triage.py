from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any

from . import db
from .governance import CONSENT_PURPOSE, CONSENT_VERSION


@dataclass(frozen=True)
class TriageStep:
    key: str
    prompt: str
    sensitive: bool = False


FLOW_LABELS = {
    "bpc": "BPC/LOAS",
    "incapacity": "Benefício por incapacidade",
    "denial_cessation": "Negativa ou cessação",
    "appeal": "Recurso administrativo",
    "revision": "Revisão de benefício",
    "autistic_child": "Atendimento para criança autista",
    "elderly": "Atendimento para pessoa idosa",
    "public_service": "Encaminhamento para serviço público",
    "labor": "Direito Trabalhista",
    "general": "Triagem jurídica geral",
}


HEALTH_CONSENT = TriageStep(
    "health_consent",
    (
        "Este é o atendimento automatizado do Bob Advogados. O BPC/LOAS é assistencial, não exige "
        "contribuição previdenciária e tem requisitos próprios. Pode haver possibilidade de análise, "
        "mas uma doença ou diagnóstico isolado não garante benefício. A avaliação depende do caso "
        "concreto, das limitações, barreiras, renda, despesas, situação familiar e documentação. "
        "Benefícios por incapacidade seguem requisitos previdenciários diferentes.\n\n"
        "Para a triagem mínima, precisamos perguntar informações sobre saúde e limitações funcionais. "
        "Esses são dados pessoais sensíveis e serão usados somente para organizar o atendimento jurídico "
        "e o encaminhamento à equipe. Você pode recusar sem impedir o atendimento humano.\n\n"
        "Se concordar com essa finalidade, responda AUTORIZO. Se não concordar, responda NÃO AUTORIZO. "
        "Não envie senha do Meu INSS, senha bancária, códigos de autenticação, CPF, RG ou fotos de documentos."
    ),
)


FLOWS: dict[str, tuple[TriageStep, ...]] = {
    "bpc": (
        HEALTH_CONSENT,
        TriageStep("beneficiary", "O atendimento é para você ou para outra pessoa?"),
        TriageStep("age", "Qual é a idade da pessoa atendida?"),
        TriageStep("request_status", "Já houve pedido no INSS? Responda: não, pendente, deferido, negado ou cessado."),
        TriageStep("cadunico", "O CadÚnico está atualizado? Responda sim, não ou não sei."),
        TriageStep("condition", "Qual condição de saúde ou deficiência é relatada?", True),
        TriageStep("functional_impact", "Em quais atividades do dia a dia, estudo, trabalho ou participação social existem limitações?", True),
        TriageStep("duration", "Há quanto tempo essas limitações existem?", True),
        TriageStep("household", "Quantas pessoas moram na mesma residência?"),
        TriageStep("income", "Qual é a renda mensal aproximada de cada pessoa da residência? Não envie comprovantes agora."),
        TriageStep("expenses", "Há gastos relevantes com medicamentos, terapias, consultas, transporte ou cuidadores? Informe apenas valores aproximados.", True),
        TriageStep("documents", "Quais documentos médicos e comprovantes de renda ou despesas existem? Diga somente quais; não envie arquivos agora."),
        TriageStep("return_period", "Qual período é melhor para o retorno humano: manhã ou tarde?"),
    ),
    "incapacity": (
        HEALTH_CONSENT,
        TriageStep("beneficiary", "O atendimento é para você ou para outra pessoa?"),
        TriageStep("work_status", "A pessoa está trabalhando, afastada ou sem atividade no momento?"),
        TriageStep("functional_impact", "Quais limitações interferem no trabalho e há quanto tempo?", True),
        TriageStep("contribution", "Houve contribuições ao INSS recentemente? Responda sim, não ou não sei."),
        TriageStep("request_status", "Já houve pedido de benefício? Responda: não, pendente, deferido, negado ou cessado."),
        TriageStep("documents", "Quais documentos médicos e profissionais existem? Diga somente quais; não envie arquivos agora."),
    ),
    "autistic_child": (
        HEALTH_CONSENT,
        TriageStep("relationship", "Qual é sua relação com a criança?"),
        TriageStep("age", "Qual é a idade da criança?"),
        TriageStep("functional_impact", "Quais barreiras ou limitações aparecem na rotina, comunicação, escola e participação social?", True),
        TriageStep("support", "Há terapias, acompanhamento escolar ou necessidade de apoio contínuo?", True),
        TriageStep("cadunico", "O CadÚnico da família está atualizado? Responda sim, não ou não sei."),
        TriageStep("household_income", "Quantas pessoas moram na residência e qual é a renda aproximada de cada uma?"),
        TriageStep("request_status", "Já houve pedido no INSS? Responda: não, pendente, deferido, negado ou cessado."),
    ),
    "elderly": (
        TriageStep("beneficiary", "O atendimento é para você ou para outra pessoa?"),
        TriageStep("age", "Qual é a idade da pessoa idosa?"),
        TriageStep("issue", "Qual é a principal situação: benefício, saúde, golpe, cuidado familiar, curatela ou outra?"),
        TriageStep("risk", "Existe risco imediato, abandono, violência ou retenção de dinheiro ou cartão?"),
        TriageStep("income", "Qual é a renda aproximada da pessoa e de quem mora com ela?"),
        TriageStep("documents", "Quais documentos ou registros da situação existem? Diga somente quais."),
    ),
    "revision": (
        TriageStep("benefit_type", "Qual benefício está sendo recebido?"),
        TriageStep("grant_date", "Em que mês e ano o benefício começou?"),
        TriageStep("reason", "O que motivou a dúvida sobre a revisão?"),
        TriageStep("prior_request", "Já houve pedido de revisão ao INSS? Responda sim, não ou não sei."),
        TriageStep("documents", "Quais cartas, extratos ou documentos do benefício existem? Diga somente quais."),
    ),
    "labor": (
        TriageStep("story", "Conte com suas palavras o que aconteceu no trabalho e se o vínculo ainda existe."),
        TriageStep("timeline", "Quais são as datas mais importantes, o que já foi tentado e qual foi a última resposta da empresa?"),
        TriageStep("documents", "Quais documentos relacionados ao caso você possui? Diga somente quais."),
        TriageStep("goal", "Qual resultado você espera e qual período é melhor para retorno: manhã ou tarde?"),
    ),
    "general": (
        TriageStep("story", "Conte com suas palavras o que aconteceu, quando começou e qual é a situação hoje."),
        TriageStep("timeline", "Quais são as datas mais importantes, o que já foi tentado e qual foi a última resposta recebida?"),
        TriageStep("documents", "Quais documentos relacionados ao caso você possui? Diga somente quais."),
        TriageStep("goal", "Qual resultado você espera e qual período é melhor para retorno: manhã ou tarde?"),
    ),
}


def _normalize(value: str) -> str:
    text = unicodedata.normalize("NFKD", value or "")
    return "".join(character for character in text if not unicodedata.combining(character)).casefold()


def detect_flow(text: str, kind: str | None = None) -> str:
    value = _normalize(text)
    if re.search(r"\b(recurso|recorrer|junta de recursos)\b", value):
        return "appeal"
    if re.search(r"\b(negad[oa]|indeferid[oa]|cessad[oa]|cortaram|suspens[oa])\b", value):
        return "denial_cessation"
    if re.search(r"\b(revisao|revisar|valor baixo|erro no calculo)\b", value):
        return "revision"
    if re.search(r"\b(crianca|filho|filha|menor)\b", value) and re.search(r"\b(autismo|autista|tea)\b", value):
        return "autistic_child"
    if re.search(r"\b(incapacidade|auxilio doenca|beneficio por incapacidade|afastamento)\b", value):
        return "incapacity"
    if kind == "bpc" or re.search(r"\b(bpc|loas|beneficio assistencial)\b", value):
        return "bpc"
    if re.search(r"\b(idos[oa]|curatela|abandono|retencao de cartao)\b", value):
        return "elderly"
    if re.search(r"\b(defensoria|cras|creas|assistencia social|servico publico)\b", value):
        return "public_service"
    if kind == "trabalhista":
        return "labor"
    return "general"


def _consent_state(text: str) -> str | None:
    value = _normalize(text)
    if re.search(r"\b(nao autorizo|recuso)\b", value):
        return "refused"
    if re.search(r"\b(autorizo|concordo)\b", value):
        return "granted"
    return None


def _step_index(flow: str, key: str | None) -> int:
    for index, step in enumerate(FLOWS.get(flow, ())):
        if step.key == key:
            return index
    return 0


def _completion_message(flow: str) -> str:
    if flow in {"bpc", "incapacity", "autistic_child"}:
        return (
            "Obrigado. A triagem inicial foi concluída e será encaminhada ao atendimento humano. "
            "As informações não confirmam direito a benefício e não substituem análise jurídica, "
            "administrativa, social ou médica do caso concreto."
        )
    return (
        "Obrigado. O relato inicial foi organizado em campos de triagem e será encaminhado à equipe. "
        "O atendimento humano continuará por este número, sem promessa de resultado."
    )


def record_immediate_handoff(conversation_id: int, flow: str, text: str) -> None:
    session = db.get_or_create_triage_session(conversation_id, flow, "initial_report")
    if session.get("status") == "active":
        db.save_triage_answer(
            int(session["id"]),
            "initial_report",
            text,
            is_sensitive=False,
            next_step=None,
            completed=True,
        )


def triage_requires_handoff(conversation_id: int) -> bool:
    sessions = db.triage_summary(conversation_id)
    return bool(sessions and sessions[0].get("status") == "completed")


def structured_triage_reply(
    conversation_id: int,
    text: str,
    kind: str | None,
    history: list[dict[str, Any]],
) -> str:
    session = db.active_triage_session(conversation_id)
    flow = str(session["flow"]) if session else detect_flow(text, kind)
    if flow in {"denial_cessation", "appeal", "public_service"}:
        record_immediate_handoff(conversation_id, flow, text)
        return ""
    steps = FLOWS.get(flow, FLOWS["general"])
    if not session:
        session = db.get_or_create_triage_session(conversation_id, flow, steps[0].key)
        prior_outbound = any(item.get("direction") == "out" and item.get("text") for item in history[:-1])
        consent = _consent_state(text) if steps[0].key == "health_consent" else None
        if consent:
            message_id = int(history[-1].get("id") or 0) if history else None
            db.record_consent(
                conversation_id=conversation_id,
                lead_id=None,
                consent_type="health_triage",
                version=CONSENT_VERSION,
                purpose=CONSENT_PURPOSE,
                status=consent,
                source="whatsapp",
                source_message_id=message_id,
            )
            if consent == "refused":
                db.save_triage_answer(
                    int(session["id"]), "health_consent", consent,
                    is_sensitive=False, next_step=None, completed=True,
                )
                return ""
            db.save_triage_answer(
                int(session["id"]), "health_consent", consent,
                is_sensitive=False, next_step=steps[1].key,
            )
            return f"Obrigado pela autorização específica. {steps[1].prompt}"
        if prior_outbound and steps[0].key != "health_consent":
            next_step = steps[1] if len(steps) > 1 else None
            db.save_triage_answer(
                int(session["id"]), steps[0].key, text,
                is_sensitive=steps[0].sensitive,
                next_step=next_step.key if next_step else None,
                completed=next_step is None,
            )
            return next_step.prompt if next_step else _completion_message(flow)
        return steps[0].prompt

    current_key = str(session.get("current_step") or "")
    index = _step_index(flow, current_key)
    current = steps[index]
    if current.key == "health_consent":
        consent = _consent_state(text)
        if not consent:
            return "Para continuar, responda somente AUTORIZO ou NÃO AUTORIZO. Você também pode pedir atendimento humano."
        message_id = int(history[-1].get("id") or 0) if history else None
        db.record_consent(
            conversation_id=conversation_id,
            lead_id=None,
            consent_type="health_triage",
            version=CONSENT_VERSION,
            purpose=CONSENT_PURPOSE,
            status=consent,
            source="whatsapp",
            source_message_id=message_id,
        )
        if consent == "refused":
            db.save_triage_answer(
                int(session["id"]), current.key, consent,
                is_sensitive=False, next_step=None, completed=True,
            )
            return ""
    next_index = index + 1
    next_step = steps[next_index] if next_index < len(steps) else None
    db.save_triage_answer(
        int(session["id"]),
        current.key,
        _consent_state(text) or text,
        is_sensitive=current.sensitive,
        next_step=next_step.key if next_step else None,
        completed=next_step is None,
    )
    if next_step:
        prefix = "Obrigado pela autorização específica. " if current.key == "health_consent" else ""
        return f"{prefix}{next_step.prompt}"
    return _completion_message(flow)
