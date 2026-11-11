from django.contrib import admin

from shop.models import Category, Customer, Order, OrderItem, Product

admin.site.register([Category, Product, Customer, Order, OrderItem])
