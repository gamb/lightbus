import argparse
import logging

from lightbus import create
from lightbus.commands.utilities import BusImportMixin, LogLevelMixin

logger = logging.getLogger(__name__)


class Command(LogLevelMixin, BusImportMixin, object):

    def setup(self, parser, subparsers):
        parser_run = subparsers.add_parser('run', help='Run Lightbus', formatter_class=argparse.ArgumentDefaultsHelpFormatter)
        self.setup_import_parameter(parser_run)

        parser_run_action_group = parser_run.add_mutually_exclusive_group()
        parser_run_action_group.add_argument('--events-only', '-E',
                                             help='Only listen for and handle events, do not respond to RPC calls',
                                             action='store_true')
        parser_run_action_group.add_argument(
            '--schema', '-m',
            help='Manually load the schema from the given file or directory. '
                 'This will normally be provided by the schema transport, '
                 'but manual loading may be useful during development or testing.',
            metavar='FILE_OR_DIRECTORY',
        )
        parser_run.set_defaults(func=self.handle)

    def handle(self, args, config, dry_run=False):
        self.setup_logging(args.log_level, config)

        bus_module = self.import_bus(args)

        bus = create(config=config)

        if args.schema:
            if args.schema == '-':
                # if '-' read from stdin
                source = None
            else:
                source = args.schema
            bus.schema.load_local(source)

        before_server_start = getattr(bus_module, 'before_server_start', None)
        if before_server_start:
            logger.debug('Calling {}.before_server_start() callback'.format(bus_module.__name__))
            before_server_start(bus)

        if dry_run:
            return

        if args.events_only:
            bus.run_forever(consume_rpcs=False)
        else:
            bus.run_forever()
