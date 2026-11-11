# shopdemo (starter)

A small coffee-shop back office built on plain Django admin. You will add
django-vantage to it, one feature per lesson.

## Setup in 5 commands

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python manage.py migrate
python manage.py seed_shop && python manage.py createsuperuser
python manage.py runserver
```

Then open http://127.0.0.1:8000/admin/ and sign in. You will see the stock Django
admin with five models: Category, Product, Customer, Order and OrderItem.

The seed command uses a fixed random seed, so everybody sees the same data: 5
categories, 10 products, 20 customers and 60 orders.

## The 5 features the lessons add, in order

1. Install django-vantage and get a full panel with zero configuration.
2. Customise a model panel in `shop/panel.py` (columns, filters, search).
3. Add live stats and charts to the Order list.
4. Add bulk actions, inline order items and read-only fields.
5. Lock down permissions and build a custom dashboard.

The finished project is in `../solution/`.
