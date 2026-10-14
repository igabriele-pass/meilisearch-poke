import json
import logging
import pathlib
import typing

import click
import sqlalchemy as sa
import sqlalchemy.orm as sa_orm

import pcapi.core.mails.transactional as transactional_mails
import pcapi.core.offerers.repository as offerers_repository
import pcapi.core.offers.api as offers_api
import pcapi.core.offers.repository as offers_repository
import pcapi.utils.cron as cron_decorators
from pcapi.connectors.big_query.importer.offer_quality import OfferQualityImporter
from pcapi.core.categories import subcategories
from pcapi.core.geography.models import Address
from pcapi.core.offerers.models import LocationType
from pcapi.core.offerers.models import Offerer
from pcapi.core.offerers.models import OffererAddress
from pcapi.core.offerers.models import Venue
from pcapi.core.offers.models import Offer
from pcapi.core.offers.models import Stock
from pcapi.models import db
from pcapi.models.feature import FeatureToggle
from pcapi.utils import regions as regions_utils
from pcapi.utils.blueprint import Blueprint


logger = logging.getLogger(__name__)

blueprint = Blueprint(__name__, __name__)


@blueprint.cli.command("reindex_recently_published_offers")
@cron_decorators.log_cron_with_transaction
@cron_decorators.cron_require_feature(FeatureToggle.ENABLE_RECURRENT_CRON)
def reindex_recently_published_offers() -> None:
    offers_api.reindex_recently_published_offers()


@blueprint.cli.command("send_future_offer_reminders")
@cron_decorators.log_cron_with_transaction
def send_future_offer_reminders() -> None:
    offers_api.send_future_offer_reminders()


@blueprint.cli.command("set_upper_timespan_of_inactive_headline_offers")
@cron_decorators.log_cron_with_transaction
def set_upper_timespan_of_inactive_headline_offers() -> None:
    offers_api.set_upper_timespan_of_inactive_headline_offers()


@blueprint.cli.command("delete_unbookable_unbooked_old_offers")
@click.argument("min_offer_id", required=False, type=int, default=None)
@click.argument("max_offer_id", required=False, type=int, default=None)
@click.argument("query_batch_size", required=False, type=int, default=5_000)
@click.argument("filter_batch_size", required=False, type=int, default=2_500)
@click.argument("delete_batch_size", required=False, type=int, default=32)
@cron_decorators.log_cron_with_transaction
def delete_unbookable_unbooked_old_offers(
    min_offer_id: int, max_offer_id: int | None, query_batch_size: int, filter_batch_size: int, delete_batch_size: int
) -> None:
    if FeatureToggle.ENABLE_OFFERS_AUTO_CLEANUP.is_active():
        offers_api.delete_unbookable_unbooked_old_offers(
            min_id=min_offer_id,
            max_id=max_offer_id,
            query_batch_size=query_batch_size,
            filter_batch_size=filter_batch_size,
            delete_batch_size=delete_batch_size,
        )
    else:
        logger.info(
            "Feature '%s' is not active. Skipping offer cleanup.", FeatureToggle.ENABLE_OFFERS_AUTO_CLEANUP.name
        )


@blueprint.cli.command("check_product_counts_consistency")
@click.argument("batch_size", required=False, type=int, default=10_000)
def check_product_counts_consistency(batch_size: int) -> None:
    product_ids = offers_api.fetch_inconsistent_products(batch_size)

    if product_ids:
        logger.error("Inconsistent product counts found", extra={"product_ids": product_ids})


@blueprint.cli.command("update_product_counts")
@click.argument("batch_size", required=False, type=int, default=10_000)
def update_product_counts(batch_size: int) -> None:
    offers_api.update_product_counts(batch_size)


@blueprint.cli.command("check_stock_quantity_consistency")
@cron_decorators.log_cron_with_transaction
def check_stock_quantity_consistency() -> None:
    inconsistent_stocks = offers_repository.check_stock_consistency()
    if inconsistent_stocks:
        logger.error("Found inconsistent stocks: %s", ", ".join([str(stock_id) for stock_id in inconsistent_stocks]))


@blueprint.cli.command("send_email_reminder_7_days_before_event")
@cron_decorators.log_cron_with_transaction
def send_email_reminder_7_days_before_event() -> None:
    """Triggers email to be sent for events happening in 7 days"""
    stocks = offers_repository.find_event_stocks_happening_in_x_days(7).options(
        sa_orm.joinedload(Stock.offer).joinedload(Offer.venue)
    )
    for stock in stocks:
        transactional_mails.send_reminder_7_days_before_event_to_pro(stock)


@blueprint.cli.command("send_email_reminder_offer_creation_j5")
@cron_decorators.log_cron_with_transaction
def send_email_reminder_offer_creation_j5_to_pro() -> None:
    """Triggers email reminder to pro 5 days after venue creation if no offer is created"""
    venues = offerers_repository.find_venues_of_offerers_with_no_offer_and_at_least_one_venue_and_validated_x_days_ago(
        5
    )
    for venue_id, venue_booking_email in venues:
        try:
            transactional_mails.send_reminder_offer_creation_j5_to_pro(venue_booking_email)
        except Exception:
            logger.exception(
                "Could not send email reminder offer creation j+5 to pro",
                extra={
                    "venue.id": venue_id,
                },
            )


@blueprint.cli.command("send_email_reminder_offer_creation_j10")
@cron_decorators.log_cron_with_transaction
def send_email_reminder_offer_creation_j10_to_pro() -> None:
    """Triggers email reminder to pro 10 days after venue creation if no offer is created"""
    venues = offerers_repository.find_venues_of_offerers_with_no_offer_and_at_least_one_venue_and_validated_x_days_ago(
        10
    )
    for venue_id, venue_booking_email in venues:
        try:
            transactional_mails.send_reminder_offer_creation_j10_to_pro(venue_booking_email)
        except Exception:
            logger.exception(
                "Could not send email reminder offer creation j+10 to pro",
                extra={
                    "venue.id": venue_id,
                },
            )


@blueprint.cli.command("update_offer_quality_scores")
@click.option("--batch-size", type=int, default=1000, help="Batch size for updates.")
@click.option("--start-from-id", type=int, default=0, help="Start updating from this offer ID.")
def update_offer_quality_scores(batch_size: int, start_from_id: int) -> None:
    importer = OfferQualityImporter()
    importer.run_offer_quality_update(batch_size=batch_size, start_from_id=start_from_id)


MUSIC_EVENT_SUBCATEGORY_IDS = (
    subcategories.CONCERT.id,
    subcategories.FESTIVAL_MUSIQUE.id,
    subcategories.EVENEMENT_MUSIQUE.id,
)
ILE_DE_FRANCE_DEPARTMENT_CODES = regions_utils.REGION_DEPARTMENT_CODES["Île-de-France"]


@blueprint.cli.command("dump_offers")
@click.argument("path", type=click.Path(dir_okay=False, writable=True, path_type=pathlib.Path))
@click.option("--venue-limit", type=int, default=1, help="Number of venues whose offers are dumped (default: 1).")
@click.option("--analyze", is_flag=True, help="Print the EXPLAIN ANALYZE plan of every query (runs each query twice).")
def dump_offers(path: pathlib.Path, venue_limit: int, analyze: bool) -> None:
    """Writes the published music events located in Île-de-France of the first venues having one, with the rows
    they depend on, into a JSON file"""
    offerers: dict[int, dict] = {}
    addresses: dict[int, dict] = {}
    venues: dict[int, dict] = {}
    offerer_addresses: dict[int, dict] = {}
    offers: dict[int, dict] = {}
    stocks: dict[int, dict] = {}

    venue_location = sa_orm.aliased(OffererAddress)
    offer_location_id = sa.func.coalesce(
        Offer.offererAddressId,
        sa.select(venue_location.id)
        .where(venue_location.venueId == Offer.venueId, venue_location.type == LocationType.VENUE_LOCATION)
        .scalar_subquery(),
    )
    music_event_offer_ids_in_ile_de_france = (
        sa.select(Offer.id)
        .join(OffererAddress, OffererAddress.id == offer_location_id)
        .join(Address, Address.id == OffererAddress.addressId)
        .where(
            Offer.subcategoryId.in_(MUSIC_EVENT_SUBCATEGORY_IDS),
            Offer.isPublished,
            Address.departmentCode.in_(ILE_DE_FRANCE_DEPARTMENT_CODES),
        )
    )

    def print_query_plan(
        connection: sa.engine.Connection,
        cursor: typing.Any,
        statement: str,
        parameters: typing.Any,
        context: typing.Any,
        executemany: bool,
    ) -> None:
        if statement.lstrip().upper().startswith("SELECT"):
            cursor.execute(f"EXPLAIN (ANALYZE, BUFFERS) {statement}", parameters)
            click.echo("\n".join(row[0] for row in cursor.fetchall()))

    if analyze:
        sa.event.listen(db.engine, "before_cursor_execute", print_query_plan)
    try:
        venue_ids = db.session.scalars(
            music_event_offer_ids_in_ile_de_france.with_only_columns(Offer.venueId)
            .distinct()
            .order_by(Offer.venueId)
            .limit(venue_limit)
        ).all()

        for venue_id in venue_ids:
            venue_offer_ids = db.session.scalars(
                music_event_offer_ids_in_ile_de_france.where(Offer.venueId == venue_id)
            ).all()
            venue_offers = db.session.scalars(
                sa.select(sa.func.row_to_json(sa.literal_column("offer")))
                .select_from(Offer)
                .where(Offer.id.in_(venue_offer_ids))
            ).all()
            venue_stocks = db.session.scalars(
                sa.select(sa.func.row_to_json(sa.literal_column("stock")))
                .select_from(Stock)
                .where(Stock.offerId.in_(venue_offer_ids))
            ).all()
            lending_venue_ids = db.session.scalars(
                sa.select(OffererAddress.venueId)
                .where(OffererAddress.id.in_([row["offererAddressId"] for row in venue_offers]))
                .distinct()
            ).all()
            venue_rows = db.session.scalars(
                sa.select(sa.func.row_to_json(sa.literal_column("venue")))
                .select_from(Venue)
                .where(Venue.id.in_([venue_id, *lending_venue_ids]))
                .execution_options(include_deleted=True)
            ).all()
            venue_offerer_addresses = db.session.scalars(
                sa.select(sa.func.row_to_json(sa.literal_column("offerer_address")))
                .select_from(OffererAddress)
                .where(OffererAddress.venueId.in_([venue_id, *lending_venue_ids]))
            ).all()
            venue_addresses = db.session.scalars(
                sa.select(sa.func.row_to_json(sa.literal_column("address")))
                .select_from(Address)
                .where(Address.id.in_([row["addressId"] for row in venue_offerer_addresses]))
            ).all()
            venue_offerers = db.session.scalars(
                sa.select(sa.func.row_to_json(sa.literal_column("offerer")))
                .select_from(Offerer)
                .where(
                    Offerer.id.in_(
                        [row["managingOffererId"] for row in venue_rows]
                        + [row["offererId"] for row in venue_offerer_addresses]
                    )
                )
            ).all()

            offerers.update({row["id"]: row for row in venue_offerers})
            addresses.update({row["id"]: row for row in venue_addresses})
            venues.update({row["id"]: row for row in venue_rows})
            offerer_addresses.update({row["id"]: row for row in venue_offerer_addresses})
            offers.update({row["id"]: row for row in venue_offers})
            stocks.update({row["id"]: row for row in venue_stocks})

            logger.info("Dumped venue %s: %s offers, %s stocks", venue_id, len(venue_offers), len(venue_stocks))
    finally:
        if analyze:
            sa.event.remove(db.engine, "before_cursor_execute", print_query_plan)

    for venue in venues.values():
        venue["venueLabelId"] = None
        venue["venueEducationalStatusId"] = None
    for offer in offers.values():
        offer["productId"] = None
        offer["authorId"] = None
        offer["lastProviderId"] = None
        offer["lastValidationAuthorUserId"] = None
    for stock in stocks.values():
        stock["priceCategoryId"] = None
        stock["lastProviderId"] = None

    dump = {
        "offerer": list(offerers.values()),
        "address": list(addresses.values()),
        "venue": list(venues.values()),
        "offerer_address": list(offerer_addresses.values()),
        "offer": list(offers.values()),
        "stock": list(stocks.values()),
    }
    path.write_text(json.dumps(dump))


@blueprint.cli.command("import_offers_dump")
@click.argument("path", type=click.Path(exists=True, dir_okay=False, path_type=pathlib.Path))
def import_offers_dump(path: pathlib.Path) -> None:
    """Inserts the rows of a `dump_offers` file, keeping their ids and skipping the existing ones"""
    dump = json.loads(path.read_text())

    for table_name in ("offerer", "address", "venue", "offerer_address", "offer", "stock"):
        db.session.execute(
            sa.text(
                f"INSERT INTO {table_name} "
                f"SELECT * FROM json_populate_recordset(CAST(NULL AS {table_name}), CAST(:rows AS json)) "
                "ON CONFLICT DO NOTHING"
            ),
            {"rows": json.dumps(dump[table_name])},
        )
        db.session.execute(sa.text(f"SELECT setval('{table_name}_id_seq', max(id)) FROM {table_name}"))

    db.session.commit()
