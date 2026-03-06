from src.tools.finance import is_direct_stock_price_query


def test_is_direct_stock_price_query_accepts_simple_quote_requests():
    assert is_direct_stock_price_query("NVDAの株価")
    assert is_direct_stock_price_query("AAPL 株価を教えて")
    assert is_direct_stock_price_query("NVIDIAの株価を見せて")


def test_is_direct_stock_price_query_rejects_non_quote_intents():
    assert not is_direct_stock_price_query("NVIDIAの株価ニュースを3つ教えて")
    assert not is_direct_stock_price_query("株価APIを作りたい")
    assert not is_direct_stock_price_query("株価とは何ですか")
    assert not is_direct_stock_price_query("NVIDIAの株価を分析して")
