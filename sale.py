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

    recreate_moves = fields.Function(fields.One2Many('stock.move', None,
        'Recreate Moves'), 'get_recreate_moves')

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
                        | ~Bool(Eval('recreate_moves', []))),
                    'depends': ['state', 'recreate_moves'],
                    },
                })

    @classmethod
    def get_recreate_moves(cls, sales, name):
        res = dict((x.id, None) for x in sales)
        for sale in sales:
            moves = []
            for line in sale.lines:
                skips = set(line.moves_ignored)
                # skips.update(line.moves_recreated)
                for move in line.moves:
                    if move.state == 'cancelled' and move in skips:
                        moves.append(move.id)
                        break
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
            picks = [shipment for shipment in [
                list(sale.shipments) + list(sale.shipment_returns)]
                if shipment.state in ['assigned', 'picked', 'packed']]
            if moves or picks:
                names = ', '.join(m.rec_name for m in (moves + picks)[:5])
                if len(names) > 5:
                    names += '...'
                raise UserError(gettext('sale_purchase_revoke.msg_can_not_revoke',
                    record=sale.rec_name,
                    names=names))

            Shipment.draft([shipment for shipment in sale.shipments
                if shipment.state == 'waiting'])
            Shipment.cancel([shipment for shipment in sale.shipments
                if shipment.state == 'draft'])
            ShipmentReturn.cancel([shipment for shipment in sale.shipment_returns
                if shipment.state == 'draft'])

            pending_moves = []
            for line in sale.lines:
                skip = set(line.moves_ignored + line.moves_recreated)
                for move in line.moves:
                    if move not in skip:
                        pending_moves.append(move.id)

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
    @ModelView.button_action('sale_purchase_revoke.act_sale_create_pending_moves_wizard')
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
            recreate_moves = sale.recreate_moves
            if not recreate_moves:
                continue

            products = dict((move.product, 0) for move in recreate_moves)
            for move in recreate_moves:
                from_uom = move.uom
                to_uom = move.product.sale_uom
                if from_uom != to_uom:
                    qty = Uom.compute_qty(from_uom, move.quantity,
                        to_uom, round=False)
                else:
                    qty = move.quantity
                products[move.product] += qty

            new_sale, = Sale.copy([sale], {'lines': []})

            for line in sale.lines:
                if line.type != 'line' or not line.product:
                    continue
                product = line.product
                if products.get(product):
                    qty = products[product]
                    Line.copy([line], {
                        'sale': new_sale,
                        'quantity': qty,
                        'uom': product.sale_uom,
                        })
            new_sales.append(new_sale)

        data = {'res_id': [s.id for s in new_sales]}
        if len(new_sales) == 1:
            action['views'].reverse()
        return action, data
