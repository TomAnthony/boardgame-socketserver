import json
import random
import sys
import os

import gevent, gevent.local, gevent.queue, gevent.server
from collections import defaultdict


class Server(object):
    def __init__(self, board, addr=None, port=None):
        self.board = board
        self.states = []
        self.local = gevent.local.local()
        self.server = None
        # player message queues
        self.players = dict((x, gevent.queue.Queue())
                            for x in xrange(1, self.board.num_players+1))
        # random player selection
        self.player_numbers = gevent.queue.JoinableQueue()

        self.addr = addr if addr is not None else '127.0.0.1'
        self.port = port if port is not None else 4242

        self.stats = {}

    def game_reset(self):
        while True:
            # initialize the game state
            del self.states[:]
            state = self.board.starting_state()
            # state = (1881801728, 11285645214616071, 2, 1)
            self.states.append(state)
            self.stats = {}

            # update all players with the starting state
            state = self.board.unpack_state(state)
            # board = self.board.get_description()
            for x in xrange(1, self.board.num_players+1):
                self.players[x].put_nowait({
                    'type': 'update',
                    'board': None,  # board,
                    'state': state,
                })

            # randomize the player selection
            players = range(1, self.board.num_players+1)
            random.shuffle(players)
            for p in players:
                self.player_numbers.put_nowait(p)

            # block until all players have terminated
            self.player_numbers.join()

    def run(self):
        game = gevent.spawn(self.game_reset)
        self.server = gevent.server.StreamServer((self.addr, self.port),
                                                 self.connection)
        print "Starting server..."
        self.server.serve_forever()

        # FIXME: need a way of nicely shutting down.
        # print "Stopping server..."
        # self.server.stop()

    def connection(self, socket, address):
        print "connection:", socket
        self.local.socket = socket
        if self.player_numbers.empty():
            self.send({
                'type': 'decline', 'message': "Game in progress."
            })
            socket.close()
            return

        self.local.run = True
        self.local.player = self.player_numbers.get()
        self.send({'type': 'player', 'message': self.local.player})

        while self.local.run:
            data = self.players[self.local.player].get()
            try:
                self.send(data)
                if data.get('winners') is not None:
                    self.local.run = False

                elif data.get('state', {}).get('player') == self.local.player:
                    message = socket.recv(8192)
                    messages = message.rstrip().split('\r\n')
                    self.parse(messages[0]) # FIXME: support for multiple messages
                                            #        or out-of-band requests
            except Exception as e:
                print e
                socket.close()
                self.player_numbers.put_nowait(self.local.player)
                self.players[self.local.player].put_nowait(data)
                self.local.run = False
        self.player_numbers.task_done()

    def parse(self, msg):
        try:
            data = json.loads(msg)

            # If we received multiple messages
            # unpack them and call them in turn
            if isinstance(data, list):
                for m in data:
                    self.parse(json.dumps(m))
            else:
                if data.get('type') == 'action':
                    self.handle_action(data.get('message'))
                elif data.get('type') == 'stats':
                    self.update_stats(data.get('message'))
                else:
                    raise Exception
        except Exception:
            print("== EXCEPTION ==")
            self.players[self.local.player].put({
                'type': 'error', 'message': msg
            })

    def update_stats(self, stats_entry):
        # with open("debug.txt", 'a') as the_file:
        #     the_file.write("stats")
        table_data = list()
        players = set()
        table_header = ['']
        table_header.extend(list(stats_entry.viewkeys()))
        player_row = defaultdict(list)

        for key, entry in stats_entry.iteritems():
            if not key in self.stats:
                self.stats[key] = {}

                for x in xrange(1, self.board.num_players+1):
                    self.stats[key][x] = []

            for player, stat in entry.iteritems():
                players.add(player)

                # player_row[player][key].append("Max", max(self.stats[key][int(player)])
                # player_row[player].append("Min", min(self.stats[key][int(player)])
                # player_row[player].append("Ave", sum(self.stats[key][int(player)]) / float(len(self.stats[key][int(player)])))
                # player_row[player].append("Sum", sum(self.stats[key][int(player)])
#                 from terminaltables import AsciiTable
# >>> table_data = [
# ... ['Heading1', 'Heading2'],
# ... ['row1 column1', 'row1 column2'],
# ... ['row2 column1', 'row2 column2'],
# ... ['row3 column1', 'row3 column2'],
# ... ]
# >>> table = AsciiTable(table_data)
# >>> print table.table
                # table_data = [
                print("Player " + str(player) + ":")
                self.stats[key][int(player)].append(stat)
                # print("\t" + str(player) + " " + str(key) + ": " + str())
                print("")
                print("\t" + str(key) + ":")
                print("\tMax: " + str(max(self.stats[key][int(player)])))
                print("\tMin: " + str(min(self.stats[key][int(player)])))
                print("\tAve: " + str(sum(self.stats[key][int(player)]) / float(len(self.stats[key][int(player)]))))
                print("\tSum: " + str(sum(self.stats[key][int(player)])))

    def handle_action(self, notation):
        # with open("debug.txt", 'a') as the_file:
        #     the_file.write("action")
        action = self.board.pack_action(notation)
        if not self.board.is_legal(self.states, action):
            self.players[self.local.player].put({
                'type': 'illegal', 'message': notation
            })
            return

        self.states.append(self.board.next_state(self.states[-1], action))
        state = self.board.unpack_state(self.states[-1])

        data = {
            'type': 'update',
            'board': None,
            'state': state,
            'last_action': {
                'player': state['previous_player'],
                'notation': notation,
                'sequence': len(self.states),
            },
        }

        if self.board.is_ended(self.states):
            data['winners'] = self.board.win_values(self.states)
            data['points'] = self.board.points_values(self.states)
            data['stats'] = self.stats

        for x in xrange(1, self.board.num_players+1):
            self.players[x].put(data)

    def send(self, data):
        self.local.socket.sendall("{0}\r\n".format(json.dumps(data)))
