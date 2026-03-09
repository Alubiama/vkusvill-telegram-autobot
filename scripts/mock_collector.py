import json
from datetime import datetime


def main() -> None:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    payload = [
        {
            "item_id": "sku-yogurt-greek",
            "name": "Йогурт греческий",
            "price": 129,
            "discount_price": 99,
            "source": f"mock_collector {now}",
        },
        {
            "item_id": "sku-chicken-fillet",
            "name": "Филе куриное",
            "price": 399,
            "discount_price": 299,
            "source": f"mock_collector {now}",
        },
        {
            "item_id": "sku-gouda",
            "name": "Сыр гауда",
            "price": 269,
            "discount_price": 209,
            "source": f"mock_collector {now}",
        },
        {
            "item_id": "sku-kefir",
            "name": "Кефир 1%",
            "price": 99,
            "discount_price": 79,
            "source": f"mock_collector {now}",
        },
        {
            "item_id": "sku-cottage-cheese",
            "name": "Творог 5%",
            "price": 139,
            "discount_price": 109,
            "source": f"mock_collector {now}",
        },
        {
            "item_id": "sku-banana",
            "name": "Бананы",
            "price": 119,
            "discount_price": 89,
            "source": f"mock_collector {now}",
        },
    ]
    print(json.dumps(payload, ensure_ascii=False))


if __name__ == "__main__":
    main()
