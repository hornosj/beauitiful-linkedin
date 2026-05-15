from beautiful_linkedin.processing.function_taxonomy import (
    JobFunction,
    classify_functions,
    primary_function,
)


def test_marketing_pt_and_en():
    assert JobFunction.MARKETING in classify_functions("Head of Marketing")
    assert JobFunction.MARKETING in classify_functions("Diretor de Marketing Digital")
    assert JobFunction.MARKETING in classify_functions("Growth Lead")


def test_sales_pt_and_en():
    assert JobFunction.SALES in classify_functions("Account Executive")
    assert JobFunction.SALES in classify_functions("Gerente Comercial")
    assert JobFunction.SALES in classify_functions("SDR at Pipefy")


def test_engineering_classifications():
    assert JobFunction.ENGINEERING in classify_functions("Software Engineer at iFood")
    assert JobFunction.ENGINEERING in classify_functions("Engenheiro de Software Sr.")
    assert JobFunction.ENGINEERING in classify_functions("DevOps Engineer")
    assert JobFunction.ENGINEERING in classify_functions("Software Analyst")
    assert JobFunction.ENGINEERING in classify_functions("Analista de Software")
    assert JobFunction.ENGINEERING in classify_functions("Systems Analyst")


def test_hr_pt_and_en():
    assert JobFunction.HR in classify_functions("People Partner")
    assert JobFunction.HR in classify_functions("Recursos Humanos")
    assert JobFunction.HR in classify_functions("Talent Acquisition Lead")


def test_multiple_functions_can_match():
    matches = classify_functions("Head of Product & Design")
    assert JobFunction.PRODUCT in matches
    assert JobFunction.DESIGN in matches


def test_primary_function_returns_first_match():
    primary = primary_function("Senior Software Engineer at Nubank")
    assert primary == JobFunction.ENGINEERING


def test_returns_empty_when_no_match():
    assert classify_functions(None) == []
    assert classify_functions("") == []
    assert classify_functions("Profissional autônomo") == []
