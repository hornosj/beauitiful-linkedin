from __future__ import annotations

from enum import Enum

from beautiful_linkedin.processing.normalizer import normalize_text


class JobFunction(str, Enum):
    MARKETING = "marketing"
    SALES = "sales"
    ENGINEERING = "engineering"
    PRODUCT = "product"
    DESIGN = "design"
    DATA = "data"
    FINANCE = "finance"
    HR = "hr"
    OPERATIONS = "operations"
    LEGAL = "legal"
    CUSTOMER_SUCCESS = "customer_success"
    EXECUTIVE = "executive"


FUNCTION_ALIASES: dict[str, list[str]] = {
    JobFunction.MARKETING.value: [
        "marketing", "growth", "demand generation", "brand", "branding",
        "comunicacao", "comunicação", "communications", "publicidade", "advertising",
        "performance marketing", "content marketing", "seo", "social media",
        "marketing digital", "digital marketing", "produto e marketing",
        "head of marketing", "diretor de marketing", "diretora de marketing", "cmo",
    ],
    JobFunction.SALES.value: [
        "sales", "vendas", "comercial", "business development", "bizdev",
        "account executive", "account manager", "sdr", "bdr",
        "inside sales", "field sales", "channel sales", "partnerships",
        "representante comercial", "executivo de contas", "executiva de contas",
        "head of sales", "diretor comercial", "diretora comercial", "vp sales",
    ],
    JobFunction.ENGINEERING.value: [
        "engineering", "engenharia", "software engineer", "engenheiro de software",
        "engenheira de software", "developer", "desenvolvedor", "desenvolvedora",
        "software analyst", "analyst software", "systems analyst", "system analyst",
        "analista de sistemas", "analista de software", "analista desenvolvedor",
        "it analyst", "technology analyst", "technical analyst", "developer analyst",
        "backend", "frontend", "full stack", "fullstack", "devops", "sre",
        "tech lead", "lider tecnico", "líder técnico", "qa", "quality assurance",
        "test engineer", "platform engineer", "cloud engineer", "mobile engineer",
        "android", "ios", "cto", "head of engineering", "vp engineering",
    ],
    JobFunction.PRODUCT.value: [
        "product manager", "gerente de produto", "product owner", "po ",
        "head of product", "vp product", "cpo", "product lead",
        "product analyst", "produto", "product",
    ],
    JobFunction.DESIGN.value: [
        "design", "designer", "ux", "ui", "product designer", "interaction designer",
        "visual designer", "brand designer", "graphic designer", "design lead",
        "head of design", "design ops",
    ],
    JobFunction.DATA.value: [
        "data scientist", "data analyst", "analytics", "ciencia de dados",
        "ciência de dados", "engenheiro de dados", "data engineer",
        "machine learning", "ml engineer", "ai engineer", "head of data",
        "business intelligence", "bi analyst", "data ops",
    ],
    JobFunction.FINANCE.value: [
        "finance", "financeiro", "financeira", "controller", "controladoria",
        "tesouraria", "treasury", "fp&a", "financial planning", "tax", "tributario",
        "tributário", "contabilidade", "accounting", "cfo", "head of finance",
        "investor relations", "relacoes com investidores", "relações com investidores",
    ],
    JobFunction.HR.value: [
        "rh", "recursos humanos", "human resources", "people", "people partner",
        "talent acquisition", "talent", "recruiter", "recruitment",
        "recrutador", "recrutadora", "people operations", "gente e gestao",
        "gente e gestão", "dei", "diversity", "chro", "head of people",
        "vp people", "people lead",
    ],
    JobFunction.OPERATIONS.value: [
        "operations", "operacoes", "operações", "ops", "supply chain",
        "logistica", "logística", "logistics", "facilities",
        "head of operations", "coo", "vp operations", "operacional",
    ],
    JobFunction.LEGAL.value: [
        "legal", "juridico", "jurídico", "advogado", "advogada", "lawyer",
        "general counsel", "compliance", "regulatory",
        "head of legal", "diretor juridico", "diretora juridica", "diretor jurídico",
        "diretora jurídica",
    ],
    JobFunction.CUSTOMER_SUCCESS.value: [
        "customer success", "customer experience", "atendimento ao cliente",
        "suporte ao cliente", "customer support", "client services",
        "implementation", "implantacao", "implantação", "onboarding",
        "head of customer success", "cs lead", "csm", "account management",
    ],
    JobFunction.EXECUTIVE.value: [
        "ceo", "chief executive", "founder", "fundador", "fundadora",
        "presidente", "president", "chairman", "chairwoman", "managing partner",
        "managing director", "general manager", "gerente geral", "country manager",
        "diretor geral", "diretora geral",
    ],
}


_FUNCTION_PATTERNS: dict[JobFunction, list[str]] = {
    JobFunction(name): [normalize_text(alias) for alias in aliases if alias.strip()]
    for name, aliases in FUNCTION_ALIASES.items()
}


def classify_functions(title: str | None) -> list[JobFunction]:
    """Return all job functions matched by ``title``.

    A title can map to multiple functions (e.g. "Head of Product & Design").
    Returned in declaration order so the first match is the primary function.
    """
    normalized = _padded(normalize_text(title))
    if not normalized.strip():
        return []
    matched: list[JobFunction] = []
    for function, patterns in _FUNCTION_PATTERNS.items():
        for pattern in patterns:
            if not pattern:
                continue
            needle = _padded(pattern)
            if needle in normalized:
                matched.append(function)
                break
    return matched


def primary_function(title: str | None) -> JobFunction | None:
    matches = classify_functions(title)
    return matches[0] if matches else None


def aliases_for(function: JobFunction) -> list[str]:
    return list(FUNCTION_ALIASES.get(function.value, []))


def _padded(value: str) -> str:
    return f" {value} "
