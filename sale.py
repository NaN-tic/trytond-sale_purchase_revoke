# This file is part of Tryton.  The COPYRIGHT file at the top level of
# this repository contains the full copyright notices and license terms.
from trytond.pool import Pool, PoolMeta
from trytond.model import Workflow, ModelView
from trytond.model import fields
from trytond.transaction import Transaction
from trytond.exceptions import UserError
from trytond.i18n import gettext
from trytond.pyson import Bool, Eval
from trytond.wizard import StateAction, Wizard


class Sale(metaclass=PoolMeta):
    __name__ = 'sale.sale'

    ignored_moves = fields.Function(fields.One2Many('stock.move', None,
        'Ignored Moves'), 'get_ignored_moves')

    @classmethod
    def __setup__(cls):
        super(Sale, cls).__setup__()
        cls._transitions |= set((
                ('confirmed', 'done'),
                ))
        cls._buttons.update({
                'revoke': {
                    'invisible': ~Eval('state').in_(
                        ['confirmed', 'processing']),
                    'depends': ['state'],
                    },
                'create_pending_moves': {
                    'invisible': (~Eval('state').in_(['processing', 'done'])
                        | ~Bool(Eval('ignored_moves', []))),
                    'depends': ['state', 'ignored_moves'],
                    },
                })

    @classmethod
    def get_ignored_moves(cls, sales, name):
        res = dict((x.id, None) for x in sales)
        for sale in sales:
            moves = []
            for line in sale.lines:
                moves += [m.id for m in line.moves_ignored]
            res[sale.id] = moves
        return res

    @classmethod
    @ModelView.button
    @Workflow.transition('done')
    def revoke(cls, sales):
        pool = Pool()
        Shipment = pool.get('stock.shipment.out')
        ShipmentReturn = pool.get('stock.shipment.out.return')
        HandleShipmentException = pool.get(
            'sale.handle.shipment.exception', type='wizard')

        def _check_moves(sale):
            moves = []
            for key in [('shipments', 'inventory_moves'),
                    ('shipment_returns', 'incoming_moves')]:
                shipments, shipment_moves = key[0], key[1]
                for shipment in getattr(sale, shipments):
                    for move in getattr(shipment, shipment_moves):
                        if move.state not in ('cancelled', 'draft', 'done'):
                            moves.append(move)
            return moves

        for sale in sales:
            moves = _check_moves(sale)
            picks = [shipment for shipment in
                list(sale.shipments) + list(sale.shipment_returns)
                if shipment.state not in ('waiting', 'draft', 'done')]
            if moves or picks:
                names = ', '.join(m.rec_name for m in (moves + picks)[:5])
                if len(names) > 5:
                    names += '...'
                raise UserError(gettext('sale_revoke.msg_can_not_revoke',
                    record=sale.rec_name,
                    names=names))

            Shipment.draft([shipment for shipment in sale.shipments
                if shipment.state == 'waiting'])
            Shipment.cancel([shipment for shipment in sale.shipments
                if shipment.state == 'draft'])
            ShipmentReturn.cancel([shipment for shipment in sale.shipment_returns
                if shipment.state == 'draft'])

            moves = [move for line in sale.lines for move in line.moves
                if move.state == 'cancelled']
            skip = set()
            for line in sale.lines:
                skip |= set(line.moves_ignored + line.moves_recreated)
            pending_moves = [x for x in moves if not x in skip]

            with Transaction().set_context({'active_id': sale.id}):
                session_id, _, _ = HandleShipmentException.create()
                handle_shipment_exception = HandleShipmentException(session_id)
                handle_shipment_exception.record = sale
                handle_shipment_exception.model = cls
                handle_shipment_exception.ask.recreate_moves = []
                handle_shipment_exception.ask.domain_moves = pending_moves
                handle_shipment_exception.transition_handle()
                HandleShipmentException.delete(session_id)

    @classmethod
    @ModelView.button_action('sale_revoke.act_sale_create_pending_moves_wizard')
    def create_pending_moves(cls, sales):
        pass


class SaleCreatePendingMoves(Wizard):
    "Sale Create Pending Moves"
    __name__ = 'sale.sale.create_pending_moves'
    start = StateAction('sale.act_sale_form')

    def do_start(self, action):
        pool = Pool()
        Uom = pool.get('product.uom')
        Sale = pool.get('sale.sale')
        Line = pool.get('sale.line')

        new_sales = []
        for sale in self.records:
            ignored_moves = sale.ignored_moves
            if not ignored_moves:
                continue

            products = dict((move.product.id, 0) for move in ignored_moves)
            sale_units = dict((move.product.id, move.product.sale_uom)
                for move in ignored_moves)

            for move in ignored_moves:
                from_uom = move.unit
                to_uom = move.product.sale_uom
                if from_uom != to_uom:
                    qty = Uom.compute_qty(from_uom, move.quantity,
                        to_uom, round=False)
                else:
                    qty = move.quantity
                products[move.product.id] += qty

            new_sale, = Sale.copy([sale], {'lines': []})

            def default_quantity(data):
                product_id = data.get('product')
                quantity = data.get('quantity')
                if product_id:
                    return products[product_id]
                return quantity

            def default_sale_unit(data):
                product_id = data.get('product')
                unit_id = data.get('unit')
                if product_id:
                    return sale_units[product_id]
                return unit_id

            Line.copy(sale.lines, default={
                'sale': new_sale,
                'quantity': default_quantity,
                'unit': default_sale_unit,
                })

            new_sales.append(new_sale)

        data = {'res_id': [s.id for s in new_sales]}
        if len(new_sales) == 1:
            action['views'].reverse()
        return action, data
