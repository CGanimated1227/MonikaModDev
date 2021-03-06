init:
    python:
        import chess
        import subprocess
        import platform
        import random
        import pygame
        import threading
        import collections
        import os

        ON_POSIX = 'posix' in sys.builtin_module_names

        def enqueue_output(out, queue, lock):
            for line in iter(out.readline, b''):
                lock.acquire()
                queue.appendleft(line)
                lock.release()
            out.close()

        def is_morning():
            return (datetime.datetime.now().time().hour > 6 and datetime.datetime.now().time().hour < 18)

        class ArchitectureError(RuntimeError):
            pass

        def is_platform_good_for_chess():
            import platform
            if platform.machine() == 'x86_64':
                return platform.system() == 'Windows' or platform.system() == 'Linux' or platform.system() == 'Darwin'
            elif platform.machine() == 'x86':
                return platform.system() == 'Windows'
            return False

        def get_mouse_pos():
            vw = config.screen_width * 10000
            vh = config.screen_height * 10000
            pw, ph = renpy.get_physical_size()
            mx, my = pygame.mouse.get_pos()

            r = None
            if vw / (vh / 10000) > pw * 10000 / ph:
                r = vw / pw
                my -= (ph - vh / r) / 2
            else:
                r = vh / ph
                mx -= (pw - vw / r) / 2

            newx = (mx * r) / 10000
            newy = (my * r) / 10000

            return (newx, newy)

        class ChessDisplayable(renpy.Displayable):
            COLOR_WHITE = True
            COLOR_BLACK = False
            MONIKA_WAITTIME = 1500
            MONIKA_STRENGTH = 12
            MONIKA_OPTIMISM = 33
            MONIKA_THREADS = 1

            def __init__(self, player_color):

                renpy.Displayable.__init__(self)

                # Some displayables we use.
                self.pieces_image = Image("mod_assets/chess_pieces.png")
                self.board_image = Image("mod_assets/chess_board.png")
                self.piece_highlight_red_image = Image("mod_assets/piece_highlight_red.png")
                self.piece_highlight_green_image = Image("mod_assets/piece_highlight_green.png")
                self.piece_highlight_yellow_image = Image("mod_assets/piece_highlight_yellow.png")
                self.piece_highlight_magenta_image = Image("mod_assets/piece_highlight_magenta.png")
                self.move_indicator_player = Image("mod_assets/move_indicator_player.png")
                self.move_indicator_monika = Image("mod_assets/move_indicator_monika.png")
                self.player_move_prompt = Text(_("It's your turn, [player]!"), size=36)
                self.num_turns = 0
                self.surrendered = False

                # The sizes of some of the images.
                self.VECTOR_PIECE_POS = {
                    'K': 0,
                    'Q': 1,
                    'R': 2,
                    'B': 3,
                    'N': 4,
                    'P': 5
                }
                self.BOARD_BORDER_WIDTH = 15
                self.BOARD_BORDER_HEIGHT = 15
                self.PIECE_WIDTH = 57
                self.PIECE_HEIGHT = 57
                self.BOARD_WIDTH = self.BOARD_BORDER_WIDTH * 2 + self.PIECE_WIDTH * 8
                self.BOARD_HEIGHT = self.BOARD_BORDER_HEIGHT * 2 + self.PIECE_HEIGHT * 8
                self.INDICATOR_WIDTH = 60
                self.INDICATOR_HEIGHT = 96

                # Stockfish engine provides AI for the game.
                # Launch the appropriate version based on the architecture and OS.
                if not is_platform_good_for_chess():
                    # This is the last-resort check, the availability of the chess game should be checked independently beforehand.
                    raise ArchitectureError('Your operating system does not support the chess game.')

                def open_stockfish(path):
                    return subprocess.Popen([renpy.loader.transfn(path)], stdin=subprocess.PIPE, stdout=subprocess.PIPE)

                if platform.system() == 'Windows':
                    if platform.machine() == 'x86':
                        self.stockfish = open_stockfish('mod_assets/stockfish_8_windows_x32.exe')
                    elif platform.machine() == 'x86_64':
                        self.stockfish = open_stockfish('mod_assets/stockfish_8_windows_x64.exe')
                elif platform.system() == 'Linux' and platform.machine() == 'x86_64':
                    self.stockfish = open_stockfish('mod_assets/stockfish_8_linux_x64')
                elif platform.system() == 'Darwin' and platform.machine() == 'x86_64':
                    self.stockfish = open_stockfish('mod_assets/stockfish_8_macosx_x64')

                # Set Monika's parameters
                self.stockfish.stdin.write("setoption name Skill Level value %d\n" % (self.MONIKA_STRENGTH))
                self.stockfish.stdin.write("setoption name Contempt value %d\n" % (self.MONIKA_OPTIMISM))

                # Set up facilities for asynchronous communication
                self.queue = collections.deque()
                self.lock = threading.Lock()
                thrd = threading.Thread(target=enqueue_output, args=(self.stockfish.stdout, self.queue, self.lock))
                thrd.daemon = True
                thrd.start()

                # Board for integration with python-chess.
                self.board = chess.Board()

                self.player_color = player_color
                self.selected_piece = None
                self.last_move_src = None
                self.last_move_dst = None
                self.possible_moves = set([])
                self.winner = None
                self.winner_confirmed = False
                self.current_turn = self.COLOR_WHITE
                self.last_clicked_king = 0.0

                # If it's Monika's turn, send her the board positions so that she can start analyzing.
                if player_color != self.COLOR_WHITE:
                    self.start_monika_analysis()

            def start_monika_analysis(self):
                self.stockfish.stdin.write("position fen %s" % (self.board.fen()) + '\n')
                self.stockfish.stdin.write("go movetime %d" % self.MONIKA_WAITTIME + '\n')

            def poll_monika_move(self):
                self.lock.acquire()
                res = None
                while self.queue:
                    line = self.queue.pop()
                    match = re.match(r"^bestmove (\w+)", line)
                    if match:
                        res = match.group(1)
                self.lock.release()
                return res

            def __del__(self):
                self.stockfish.stdin.close()
                self.stockfish.wait()

            @staticmethod
            def coords_to_uci(x, y):
                x = chr(x + ord('a'))
                y += 1
                return str(x) + str(y)

            def check_winner(self, current_move):
                if self.board.is_game_over():
                    if self.board.result() == '1/2-1/2':
                        self.winner = 'none'
                    else:
                        self.winner = current_move

            # Renders the board, pieces, etc.
            def render(self, width, height, st, at):

                # Poll Monika for moves if it's her turn
                if self.current_turn != self.player_color:
                    monika_move = self.poll_monika_move()
                    if monika_move is not None:
                        self.last_move_src = (ord(monika_move[0]) - ord('a'), ord(monika_move[1]) - ord('1'))
                        self.last_move_dst = (ord(monika_move[2]) - ord('a'), ord(monika_move[3]) - ord('1'))
                        self.board.push_uci(monika_move)
                        if self.current_turn == self.COLOR_BLACK:
                            self.num_turns += 1
                        self.current_turn = self.player_color
                        self.check_winner('monika')

                # The Render object we'll be drawing into.
                r = renpy.Render(width, height)

                # Prepare the board as a renderer.
                board = renpy.render(self.board_image, 1280, 720, st, at)

                # Prepare the pieces vector as a renderer.
                pieces = renpy.render(self.pieces_image, 1280, 720, st, at)

                # Prepare the highlights as a renderers.
                highlight_red = renpy.render(self.piece_highlight_red_image, 1280, 720, st, at)
                highlight_green = renpy.render(self.piece_highlight_green_image, 1280, 720, st, at)
                highlight_yellow = renpy.render(self.piece_highlight_yellow_image, 1280, 720, st, at)
                highlight_magenta = renpy.render(self.piece_highlight_magenta_image, 1280, 720, st, at)

                # Draw the board.
                r.blit(board, (int((width - self.BOARD_WIDTH) / 2), int((height - self.BOARD_HEIGHT) / 2)))

                indicator_position = (int((width - self.INDICATOR_WIDTH) / 2 + self.BOARD_WIDTH / 2 + 50),
                                      int((height - self.INDICATOR_HEIGHT) / 2))

                # Draw the move indicator
                if self.current_turn == self.player_color:
                    r.blit(renpy.render(self.move_indicator_player, 1280, 720, st, at), indicator_position)
                else:
                    r.blit(renpy.render(self.move_indicator_monika, 1280, 720, st, at), indicator_position)

                def get_piece_render_for_letter(letter):
                    jy = 0 if letter.islower() else 1
                    jx = self.VECTOR_PIECE_POS[letter.upper()]
                    return pieces.subsurface((jx * self.PIECE_WIDTH, jy * self.PIECE_HEIGHT,
                                              self.PIECE_WIDTH, self.PIECE_HEIGHT))

                mx, my = get_mouse_pos()

                # Draw the pieces on the Board renderer.
                for ix in range(8):
                    for iy in range(8):
                        iy_orig = iy
                        if self.player_color == self.COLOR_WHITE:
                            iy = 7 - iy
                        x = int((width - (self.BOARD_WIDTH - self.BOARD_BORDER_WIDTH * 2)) / 2  + ix * self.PIECE_WIDTH)
                        y = int((height - (self.BOARD_HEIGHT - self.BOARD_BORDER_HEIGHT * 2)) / 2 + iy * self.PIECE_HEIGHT)

                        def render_move(move):
                            if move is not None and ix == move[0] and iy_orig == move[1]:
                                if self.player_color == self.current_turn:
                                    r.blit(highlight_magenta, (x, y))
                                else:
                                    r.blit(highlight_green, (x, y))

                        render_move(self.last_move_src)
                        render_move(self.last_move_dst)

                        # Take care not to render the selected piece twice.
                        if (self.selected_piece is not None and
                            ix == self.selected_piece[0] and
                            iy_orig == self.selected_piece[1]):
                            r.blit(highlight_green, (x, y))
                            continue

                        piece = self.board.piece_at(iy_orig * 8 + ix)

                        possible_move_str = None
                        if self.possible_moves:
                            possible_move_str = (ChessDisplayable.coords_to_uci(self.selected_piece[0], self.selected_piece[1]) +
                                                 ChessDisplayable.coords_to_uci(ix, iy_orig))
                        if (self.possible_moves and
                            chess.Move.from_uci(possible_move_str) in self.possible_moves):
                            r.blit(highlight_yellow, (x, y))

                        if piece is None:
                            continue

                        if (mx >= x and mx < x + self.PIECE_WIDTH and
                            my >= y and my < y + self.PIECE_HEIGHT and
                            bool(str(piece).isupper()) == (self.player_color == self.COLOR_WHITE) and
                            self.current_turn == self.player_color and
                            self.selected_piece is None and
                            not self.winner):
                            r.blit(highlight_green, (x, y))

                        if str(piece).lower() == 'k' and self.winner:
                            winner_color = None
                            if self.winner == 'player':
                                winner_color = self.player_color
                            elif self.winner == 'monika':
                                winner_color = not self.player_color
                            if bool(str(piece).islower()) == winner_color:
                                r.blit(highlight_red, (x, y))

                        r.blit(get_piece_render_for_letter(str(piece)), (x, y))


                if self.current_turn == self.player_color and self.winner is None:
                    # Display the indication that it's the player's turn
                    prompt = renpy.render(self.player_move_prompt, 1280, 720, st, at)
                    pw, ph = prompt.get_size()
                    bh = (height - self.BOARD_HEIGHT) / 2
                    r.blit(prompt, (int((width - pw) / 2), int(self.BOARD_HEIGHT + bh + (bh - ph) / 2)))

                if self.selected_piece is not None:
                    # Draw the selected piece.
                    piece = self.board.piece_at(self.selected_piece[1] * 8 + self.selected_piece[0])
                    assert piece is not None
                    px, py = get_mouse_pos()
                    px -= self.PIECE_WIDTH / 2
                    py -= self.PIECE_HEIGHT / 2
                    r.blit(get_piece_render_for_letter(str(piece)), (px, py))

                # Ask that we be re-rendered ASAP, so we can show the next frame.
                renpy.redraw(self, 0)

                # Return the Render object.
                return r

            # Handles events.
            def event(self, ev, x, y, st):

                def get_piece_pos():
                    mx, my = get_mouse_pos()
                    mx -= (1280 - (self.BOARD_WIDTH - self.BOARD_BORDER_WIDTH * 2)) / 2
                    my -= (720 - (self.BOARD_HEIGHT - self.BOARD_BORDER_HEIGHT * 2)) / 2
                    px = mx / self.PIECE_WIDTH
                    py = my / self.PIECE_HEIGHT
                    if self.player_color == self.COLOR_WHITE:
                        py = 7 - py
                    if py >= 0 and py < 8 and px >= 0 and px < 8:
                        return (px, py)
                    return (None, None)

                # Mousebutton down == possibly select the piece to move
                if ev.type == pygame.MOUSEBUTTONDOWN and ev.button == 1:
                    if self.winner and not self.winner_confirmed:
                        self.winner_confirmed = True
                    else:
                        px, py = get_piece_pos()
                        if (px is not None and py is not None and
                            self.board.piece_at(py * 8 + px) is not None and
                            bool(str(self.board.piece_at(py * 8 + px)).isupper()) == (self.player_color == self.COLOR_WHITE) and
                            self.current_turn == self.player_color):

                            piece = str(self.board.piece_at(py * 8 + px))
                            if piece.lower() == 'k' and piece.islower() == (self.player_color == self.COLOR_BLACK):
                                if st - self.last_clicked_king < 0.2:
                                    self.winner = 'monika'
                                    self.winner_confirmed = True
                                    self.surrendered = True
                                self.last_clicked_king = st

                            src = ChessDisplayable.coords_to_uci(px, py)

                            all_moves = [chess.Move.from_uci(src + ChessDisplayable.coords_to_uci(file, rank))
                                                                for file in range(8)
                                                                for rank in range(8)]
                            self.possible_moves = (set(self.board.legal_moves).intersection(all_moves))
                            self.selected_piece = (px, py)

                # Mousebutton up == possibly release the selected piece
                if ev.type == pygame.MOUSEBUTTONUP and ev.button == 1:
                    px, py = get_piece_pos()
                    if px is not None and py is not None and self.selected_piece is not None:
                        move_str = self.coords_to_uci(self.selected_piece[0], self.selected_piece[1]) + self.coords_to_uci(px, py)
                        if chess.Move.from_uci(move_str) in self.possible_moves:
                            self.last_move_src = self.selected_piece
                            self.last_move_dst = (px, py)
                            self.board.push_uci(move_str)
                            self.check_winner('player')
                            if self.current_turn == self.COLOR_BLACK:
                                self.num_turns += 1
                            self.current_turn = not self.current_turn
                            self.start_monika_analysis()
                    self.selected_piece = None
                    self.possible_moves = set([])

                # If we have a winner, return him or her. Otherwise, ignore the current event.
                if self.winner and self.winner_confirmed:
                    return (self.winner, self.surrendered, self.num_turns)
                else:
                    raise renpy.IgnoreEvent()


label game_chess:
    hide screen keylistener
    m 1b "You want to play chess? Alright~"
    m 1a "Get ready!"
    call demo_minigame_chess from _call_demo_minigame_chess
    return

label demo_minigame_chess:
    menu:
        m "What color would suit you?"

        "White":
            $ player_color = ChessDisplayable.COLOR_WHITE
        "Black":
            $ player_color = ChessDisplayable.COLOR_BLACK
        "Let's draw lots!":
            $ choice = random.randint(0, 1) == 0
            if choice:
                $ player_color = ChessDisplayable.COLOR_WHITE
                m 2a "Oh look, I drew black! Let's begin!"
            else:
                $ player_color = ChessDisplayable.COLOR_BLACK
                m 2a "Oh look, I drew white! Let's begin!"

    window hide None

    python:
        ui.add(ChessDisplayable(player_color))
        winner, surrendered, num_turns = ui.interact(suppress_overlay=True, suppress_underlay=True)

    #Regenerate the spaceroom scene
    $scene_change=True #Force scene generation
    call spaceroom from _call_spaceroom

    if winner == "monika":
        if surrendered and num_turns <= 4:
            m 1e "Come on, don't give up so easily."
        else:
            m 1b "I win!"

    elif winner == "player":

        m 2a "You won! Congratulations."

    else:

        m "A draw? How boring..."

    menu:
        m "Do you want to play again?"

        "Yes.":
            jump demo_minigame_chess
        "No.":

            if winner == "monika":
                m 2d "Despite its simple rules, chess is a really intricate game."
                m 1a "It's okay if you find yourself struggling at times."
                m 1j "Remember, the important thing is to be able to learn from your mistakes."
            else:
                m 2b "It's amazing how much more I have to learn even now."
                m 2a "I really don't mind losing as long as I can learn something."
                m 1j "After all, the company is good."

    return
