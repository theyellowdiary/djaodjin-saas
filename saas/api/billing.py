# Copyright (c) 2018, DjaoDjin inc.
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#
# 1. Redistributions of source code must retain the above copyright notice,
#    this list of conditions and the following disclaimer.
# 2. Redistributions in binary form must reproduce the above copyright
#    notice, this list of conditions and the following disclaimer in the
#    documentation and/or other materials provided with the distribution.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS
# "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED
# TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR
# PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR
# CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL,
# EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO,
# PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS;
# OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY,
# WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR
# OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF
# ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

"""
APIs for cart and checkout functionality.
"""
from __future__ import unicode_literals

import csv, logging

from django.core.exceptions import MultipleObjectsReturned
from django.contrib import messages
from django.shortcuts import get_object_or_404
from django.utils.translation import ugettext_lazy as _
from rest_framework.generics import (CreateAPIView, DestroyAPIView,
    GenericAPIView, RetrieveAPIView)
from rest_framework.mixins import CreateModelMixin
from rest_framework.response import Response
from rest_framework import serializers, status

from ..backends import ProcessorError
from ..compat import is_authenticated
from ..docs import swagger_auto_schema, OpenAPIResponse
from ..mixins import CartMixin, OrganizationMixin
from ..models import CartItem
from .serializers import (ChargeSerializer, InvoicableSerializer,
    NoModelSerializer, PlanRelatedField, ValidationErrorSerializer)

#pylint: disable=no-init,old-style-class
LOGGER = logging.getLogger(__name__)


class CartItemCreateSerializer(serializers.ModelSerializer):
    """
    Serializer to build a request.user set of plans to subscribe to (i.e. cart).
    """
    plan = PlanRelatedField(read_only=False, required=True,
        help_text=_("The plan to add into the request.user cart."))

    class Meta:
        model = CartItem
        fields = ('plan', 'option', 'first_name', 'last_name', 'sync_on')


class OrganizationCartSerializer(NoModelSerializer):
    """
    Items which will be charged on an order checkout action.
    """
    items = InvoicableSerializer(many=True)


class CheckoutItemSerializer(NoModelSerializer):
    option = serializers.IntegerField(
        help_text=_("selected plan option during checkout"))


class CheckoutSerializer(NoModelSerializer):
    """
    Processor token to charge the cart items.
    """
    items = CheckoutItemSerializer(required=False, many=True)
    remember_card = serializers.BooleanField(required=False,
        help_text=_("attaches the payment card to the Organization when true"))
    processor_token = serializers.CharField(required=False, max_length=255,
        help_text=_("one-time token generated by the processor"\
            "from the payment card."))
    street_address = serializers.CharField(required=False, allow_blank=True,
        help_text=_("Street address"))
    locality = serializers.CharField(required=False, allow_blank=True,
        help_text=_("City/Town"))
    region = serializers.CharField(required=False, allow_blank=True,
        help_text=_("State/Province/County"))
    postal_code = serializers.CharField(required=False, allow_blank=True,
        help_text=_("Zip/Postal code"))
    country = serializers.CharField(required=False, allow_blank=True,
        help_text=_("Country"))


class CartItemAPIView(CartMixin, CreateAPIView):
    """
    Adds a ``Plan`` into the cart of the ``request.user``.

    The cart can later be checked out and paid by an ``Organization``,
    either through the :ref:`HTML page<pages_cart>`
    or :ref:`API end point<api_checkout>`.

    This end point is typically used when a user is presented with a list
    of add-ons that she can subscribes to in one checkout screen. The end-point
    works in both cases, authenticated or anonymous users. For authenticated
    users, the cart is stored in the database as ``CartItem`` objects.
    For anonymous users, the cart is stored in an HTTP Cookie.

    The end-point accepts a single item or a list of items.

    ``quantity`` is optional. When it is not specified, subsquent checkout
    screens will provide choices to pay multiple periods in advance
    When additional ``first_name``, ``last_name`` and ``sync_on`` are specified,
    payment can be made by one ``Organization`` for another ``Organization``
    to be subscribed (see :ref:`GroupBuy orders<group_buy>`).

    **Examples

    .. code-block:: http

        POST /api/cart/ HTTP/1.1

    .. code-block:: json

        {
            "plan": "open-space",
            "option": 1
        }

    responds

    .. code-block:: json

        {
            "plan": "open-space",
            "option": 1
        }

    ``option`` is optional. When it is not specified, subsquent checkout
    screens will provide choices to pay multiple periods in advance
    When additional ``first_name``, ``last_name`` and ``sync_on`` are specified,
    payment can be made by one ``Organization`` for another ``Organization``
    to be subscribed (see :ref:`GroupBuy orders<group_buy>`).
    """
    #pylint: disable=no-member

    model = CartItem
    serializer_class = CartItemCreateSerializer

    # XXX This was a workaround until we figure what is wrong with proxy
    # and csrf, unfortunately it prevents authenticated users to add into
    # their db cart, instead put their choices into the unauth session.
    # authentication_classes = []
    def post(self, request, *args, **kwargs):
        items = None
        serializer = self.get_serializer(data=request.data)
        if serializer.is_valid():
            items = [serializer.validated_data]
        else:
            serializer = self.get_serializer(data=request.data, many=True)
            if serializer.is_valid():
                items = serializer.validated_data
        if not items:
            return Response(
                serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        cart_items = []
        status_code = status.HTTP_200_OK
        for item in items:
            cart_item, created = self.insert_item(request, **item)
            if created:
                status_code = status.HTTP_201_CREATED
            # insert_item will either return a dict or a CartItem instance
            # (which cannot be directly serialized).
            if isinstance(cart_item, CartItem):
                cart_items += [serializer.to_representation(cart_item)]
            else:
                cart_items += [cart_item]
        if len(items) > 1:
            headers = self.get_success_headers(cart_items)
            return Response(cart_items, status=status_code, headers=headers)
        headers = self.get_success_headers(cart_items[0])
        return Response(cart_items[0], status=status_code, headers=headers)


class CartItemUploadSerializer(NoModelSerializer):

    created = serializers.CharField()
    updated = serializers.CharField()
    failed = serializers.CharField()


class CartItemUploadAPIView(CartMixin, GenericAPIView):
    """
    Add a ``Plan`` into the subscription cart of multiple users as per the
    content of an uploaded file.

    This works bulk fashion of :ref:`/cart/ endpoint<api_cart>`. The
    uploaded file must be a CSV containing the fields ``first_name``,
    ``last_name`` and email. The CSV file must not contain a header
    line, only data.

    **Examples

    Content of ``names.csv``:

    .. sourcecode

        Joe,Smith,joesmith@example.com
        Marie,Johnson,mariejohnson@example.com

    .. code-block:: http

        POST /api/cart/:plan/upload/ HTTP/1.1

        Content-Disposition: form-data; name="file"; filename="names.csv"
        Content-Type: text/csv

    responds

    .. code-block:: json

        {
            "created" [
                {
                    "first_name": "Joe",
                    "last_name": "Smith",
                    "email": "joesmith@example.com"
                },
                {
                    "first_name": "Marie",
                    "last_name": "Johnson",
                    "email": "mariejohnson@example.com"
                }
            ],
            "updated": [],
            "failed": []
        }
    """
    serializer_class = CartItemUploadSerializer

    def post(self, request, *args, **kwargs):
        #pylint:disable=unused-argument
        plan = kwargs.get('plan')
        filed = csv.reader(request.FILES['file'])
        response = {'created': [],
                    'updated': [],
                    'failed': []}

        for row in filed:
            try:
                first_name, last_name, email = row
            except csv.Error:
                response['failed'].append({'data': {'raw': row},
                                           'error': 'Unable to parse row'})
            else:
                serializer = CartItemCreateSerializer(
                    data={'plan': plan,
                          'first_name': first_name,
                          'last_name': last_name,
                          'sync_on': email})
                if serializer.is_valid():
                    cart_item, created = self.insert_item(
                        request, **serializer.data)
                    if isinstance(cart_item, CartItem):
                        cart_item = serializer.to_representation(cart_item)
                    if created:
                        response['created'].append(cart_item)
                    else:
                        response['updated'].append(cart_item)
                else:
                    response['failed'].append({'data': serializer.data,
                                               'error': serializer.errors})

        return Response(response)


class CartItemDestroyAPIView(DestroyAPIView):
    """
    Remove a ``Plan`` from the subscription cart of the ``request.user``.

    **Examples

    .. code-block:: http

        DELETE /api/cart/open-space/ HTTP/1.1
    """

    model = CartItem

    @staticmethod
    def destroy_in_session(request, *args, **kwargs):
        #pylint: disable=unused-argument
        cart_items = []
        if 'cart_items' in request.session:
            cart_items = request.session['cart_items']
        candidate = kwargs.get('plan')
        serialized_cart_items = []
        found = False
        for item in cart_items:
            if item['plan'] == candidate:
                found = True
                continue
            serialized_cart_items += [item]
        request.session['cart_items'] = serialized_cart_items
        return found

    def get_object(self):
        result = None
        try:
            result = get_object_or_404(CartItem,
                plan__slug=self.kwargs.get('plan'),
                user=self.request.user, recorded=False)
        except MultipleObjectsReturned as err:
            # This should not happen but in case the db is corrupted,
            # we want to do something acceptable to the user.
            LOGGER.exception(err)
            result = CartItem.objects.filter(
                plan__slug=self.kwargs.get('plan'),
                user=self.request.user, recorded=False).first()
        return result

    def delete(self, request, *args, **kwargs):
        destroyed = self.destroy_in_session(request, *args, **kwargs)
        # We found the items in the session cart, nothing else to do.
        if not destroyed and is_authenticated(self.request):
            # If the user is authenticated, we delete the cart items
            # from the database.
            return self.destroy(request, *args, **kwargs)
        return Response(status=status.HTTP_204_NO_CONTENT)


class RedeemCouponSerializer(NoModelSerializer):
    """
    Serializer to redeem a ``Coupon``.
    """

    code = serializers.CharField(help_text=_("Coupon code to redeem"))

    def create(self, validated_data):
        return validated_data


class CouponRedeemAPIView(GenericAPIView):
    """
    Redeems a ``Coupon`` and applies the discount to the eligible items
    in the cart.

    **Examples

    .. code-block:: http

         POST /api/redeem HTTP/1.1

    .. code-block:: json

        {
            "code": "LABORDAY"
        }

    responds

    .. code-block:: json

        {
            "details": "Coupon 'LABORDAY' was successfully applied."
        }
    """
    serializer_class = RedeemCouponSerializer

    # XXX This is not a ValidationErrorSerializer but we return a message.
    # XXX Should many return the updated cart but we are dealing with users,
    # not organizations here.
    @swagger_auto_schema(responses={
        200: OpenAPIResponse("", ValidationErrorSerializer)})
    def post(self, request, *args, **kwargs): #pylint: disable=unused-argument
        serializer = self.get_serializer(data=request.data)
        if serializer.is_valid():
            coupon_code = serializer.data['code']
            if CartItem.objects.redeem(request.user, coupon_code):
                details = {"details": (
                    _("Coupon '%s' was successfully applied.") % coupon_code)}
                headers = {}
                # XXX Django 1.7: 500 error, argument must be an HttpRequest
                # object, not 'Request'. Not an issue with Django 1.6.2
                # Since we rely on the message to appear after reload of
                # the cart page in the casperjs tests, we can't get rid
                # of this statement just yet.
                messages.success(request._request, details['details'])#pylint: disable=protected-access
                return Response(details, status=status.HTTP_200_OK,
                                headers=headers)
            details = {"details": (
                _("No items can be discounted using this coupon: %(code)s.") % {
                'code': coupon_code})}
            return Response(details, status=status.HTTP_400_BAD_REQUEST)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class CheckoutAPIView(CartMixin, OrganizationMixin,
                      CreateModelMixin, RetrieveAPIView):
    """
    Get a list indexed by plans of items that will be charged
    (`lines`) and options that could be charged instead.

    In many subscription businesses, it is possible to buy multiple
    period in advance at a discount. The options reflects that.

    **Examples

    .. code-block:: http

        GET /api/billing/xia/checkout HTTP/1.1

    responds

    .. code-block:: json

        {"items":
        [{
          "subscription":{
              "created_at":"2016-06-21T23:24:09.242925Z",
              "ends_at":"2016-10-21T23:24:09.229768Z",
              "description":null,
              "organization":{
                  "slug":"xia",
                  "full_name":"Xia",
                  "printable_name":"Xia",
                  "created_at":"2012-08-14T23:16:55Z",
                  "email":"xia@localhost.localdomain"
              },
              "plan":{
                  "slug":"basic",
                  "title":"Basic",
                  "description":"Basic Plan",
                  "is_active":true,
                  "setup_amount":0,
                  "period_amount":2000,
                  "interval":4,
                  "app_url":"/app/"
              },
              "auto_renew":true
          },
          "lines":[{
              "created_at":"2016-06-21T23:42:13.863739Z",
              "description":"Subscription to basic until 2016/11/21 (1 month)",
              "amount":"$20.00",
              "is_debit":false,
              "orig_account":"Receivable",
              "orig_organization":"cowork",
              "orig_amount":2000,
              "orig_unit":"usd",
              "dest_account":"Payable",
              "dest_organization":"xia",
              "dest_amount":2000,
              "dest_unit":"usd"
          }],
          "options":[]
        }]
        }
    """
    # XXX replace key `items` by `results` to match other serializers?
    serializer_class = OrganizationCartSerializer

    @swagger_auto_schema(request_body=CheckoutSerializer, responses={
        201: OpenAPIResponse("", ChargeSerializer)})
    def post(self, request, *args, **kwargs):
        """
        Places an order for the subscription items in the cart and creates
        a ``Charge`` on the ``{organization}`` payment card.

        If the charge fails a balance is due, to be collected later.

        The cart is manipulated through various API endpoints:

        - `/api/cart/redeem/` applies a coupon code for a potential discount.
        - `/api/cart/` adds or updates a cart item.
        - `/api/cart/{plan}` removes a cart item.

        **Examples

        .. code-block:: http

            POST /api/billing/xia/checkout

            {
                "remember_card": true,
                "processor_token": "tok_23prgoqpstf56todq"
            }

        responds

        .. code-block:: json

           {
                "created_at": "2016-06-21T23:42:44.270977Z",
                "processor_key": "pay_5lK5TacFH3gbKe"
                "amount": 2000,
                "unit": "usd",
                "description": "Charge pay_5lK5TacFH3gblP on credit card"\
" of Xia",
                "last4": "1234",
                "exp_date": "2016-06-01",
                "state": "created"
            }
        """
        return self.create(request, *args, **kwargs)

    def get_queryset(self):
        return super(CheckoutAPIView, self).as_invoicables(
            self.request.user, self.organization)

    def get(self, request, *args, **kwargs):
        queryset = self.get_queryset()
        serializer = self.get_serializer({'items': queryset})
        return Response(serializer.data)

    def create(self, request, *args, **kwargs):#pylint:disable=unused-argument
        serializer = CheckoutSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        queryset = self.get_queryset()
        items_options = data.get('items')
        if items_options:
            for index, item in enumerate(items_options):
                opt_index = item['option'] - 1
                if index >= len(queryset):
                    continue
                if opt_index >= len(queryset[index]['options']):
                    continue
                selected = queryset[index]['options'][opt_index]
                queryset[index]['lines'].append(selected)
        self.organization.update_address_if_empty(country=data.get('country'),
            region=data.get('region'), locality=data.get('locality'),
            street_address=data.get('street_address'),
            postal_code=data.get('postal_code'))

        try:
            charge = self.organization.checkout(
                queryset, self.request.user,
                token=data.get('processor_token'),
                remember_card=data.get('remember_card', False))
            if charge and charge.invoiced_total.amount > 0:
                result = ChargeSerializer(charge)
                return Response(result.data, status=status.HTTP_200_OK)
        except ProcessorError as err:
            return Response({"details": err}, status=status.HTTP_403_FORBIDDEN)
        return Response({}, status=status.HTTP_200_OK)
