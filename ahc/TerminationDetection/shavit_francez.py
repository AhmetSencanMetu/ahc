import sys
import queue
import random
from enum import Enum
from datetime import datetime as dt

from ahc.LinkLayers.GenericLinkLayer import LinkLayer
from ahc.Routing.AllSeeingEyeNetworkLayer import AllSeingEyeNetworkLayer
from ahc.Ahc import (ComponentModel, Event, ConnectorTypes, ComponentRegistry, GenericMessagePayload, GenericMessageHeader,
                 GenericMessage, EventTypes)

WAVE_DEBUG = False

registry = ComponentRegistry()

# define your own message types
class ApplicationLayerMessageType(Enum):
    BASIC = "basic"
    CONTROL = "control" # NOTE: Means the acknowledgement message!
    WAVE = "wave"

class SFWaveMessageType(Enum):
    REQUEST = "req"
    RESPONSE = "resp"

    def __str__(self):
        return self.name

class SFWaveResponse(Enum):
    FINISHED = "fin"
    ACTIVE = "act"

    def __str__(self):
        return self.name

class SFWaveMessagePayload:
    tag: int
    type: SFWaveMessageType
    response: SFWaveResponse

    def __init__(self, tag: int, typ: SFWaveMessageType, resp: SFWaveResponse = None) -> None:
        self.tag = tag
        self.response = resp
        self.type = typ

class SFAHCNodeSimulationStatus(Enum):
    ACTIVE = "active"
    PASSIVE = "passive"
    OUT_OF_CLOCK = "ooc"
    OUT_OF_TREE = "oot"

    def __str__(self):
        return self.name

# define your own message header structure
class ApplicationLayerMessageHeader(GenericMessageHeader):
    pass

# define your own message payload structure
class ApplicationLayerMessagePayload(GenericMessagePayload):
    pass

class ShavitFrancezApplicationLayerComponent(ComponentModel):
    def __init__(self, componentname, componentinstancenumber, context):
        super().__init__(componentname, componentinstancenumber, context=context)

        self.context = context
        # self.eventhandlers[ApplicationLayerMessageType.BASIC] = self.on_basic_message
        # self.eventhandlers[ApplicationLayerMessageType.CONTROL] = self.on_control_message

        self.basic_message_queue = queue.Queue(maxsize=-1)
        self.control_message_queue = queue.Queue(maxsize=-1)
        self.simulation_state = SFAHCNodeSimulationStatus.PASSIVE

        self.sleep_ms_per_tick = context["ms_per_tick"]
        self.simulation_ticks_total = context["simulation_ticks"]
        self.alive_for_next_ticks = context["initial_liveness"][componentinstancenumber]
        self.communication_on_active_prob = context["communication_on_active_prob"]
        self.min_activeness_after_receive = context["min_activeness_after_receive"]
        self.max_activeness_after_receive = context["max_activeness_after_receive"]
        self.package_process_per_tick = context["node_package_process_per_tick"]
        self.die_passiveness_threshold = context["passiveness_death_thresh"]

        if context["hard_stop_on_tick"] is None:
            self.hard_stop_on_tick = None
        else:
            self.hard_stop_on_tick = context["hard_stop_on_tick"][self.componentinstancenumber]

        self.__tick_n = 0
        self._passive_counter = 0

        self._child_counter = 0
        self._parent_node = None
        self._in_tree = False
        self._children = []
        
        self._cms = 0   # control messages sent
        self._wms = 0   # wave messages sent

        self._i_am_root = context["network"].root == self.componentinstancenumber

        if context["only_root_alive_initially"]:
            if self._i_am_root:
                self.alive_for_next_ticks = 20 # this may change though...
                self._in_tree = True
            else:
                self.alive_for_next_ticks = 0
        elif self._i_am_root and self.alive_for_next_ticks == 0:
            self.alive_for_next_ticks = 20 # this may change though...

        if self.alive_for_next_ticks > 0:
            self.simulation_state = SFAHCNodeSimulationStatus.ACTIVE

        self.__exited_from_tree = False
        self.my_wave_bucket = []
        self.announce_on_next_tick = False

    def prepare_application_layer_message(self, message_type: ApplicationLayerMessageType, destination_node_id: int, payload: object) -> GenericMessage:
        hdr = ApplicationLayerMessageHeader(message_type, self.componentinstancenumber, destination_node_id)
        payload = ApplicationLayerMessagePayload(payload)
        
        return GenericMessage(hdr, payload)

    def send_random_basic_message(self, to: int) -> None:
        self._child_counter += 1
        self._children.append(to)
        self.send_down(Event(self, EventTypes.MFRT, self.prepare_application_layer_message(ApplicationLayerMessageType.BASIC, to, str(dt.now().timestamp()))))

    def send_ack_control_message(self, to: int, is_dead: bool) -> None:
        # print(f"send_ack_control_message: N-{self.componentinstancenumber} ==> N-{to} ({self._parent_node}) : {'DEAD' if is_dead else 'PKG_RESP'}")
        self.send_down(Event(self, EventTypes.MFRT, self.prepare_application_layer_message(ApplicationLayerMessageType.CONTROL, to, str(dt.now().timestamp()))))
        self._cms += 1

    def on_init(self, eventobj: Event):
        # print(f"Initializing {self.componentname}.{self.componentinstancenumber}")
        pass

    def on_message_from_bottom(self, eventobj: Event):
        applmessage = eventobj.eventcontent
        hdr = applmessage.header

        # print(f"Node-{self.componentinstancenumber}: Node-{hdr.messagefrom} has sent {hdr.messagetype} message (payload: {applmessage.payload})")

        if hdr.messagetype == ApplicationLayerMessageType.BASIC:
            self.basic_message_queue.put_nowait(applmessage)

            if self._in_tree:
                self.send_ack_control_message(hdr.messagefrom, False)
            else:
                self._parent_node = hdr.messagefrom
                self._in_tree = True

                if self.componentinstancenumber not in self.context["alive_nodes"]:
                    self.context["alive_nodes"].append(self.componentinstancenumber)
        elif hdr.messagetype == ApplicationLayerMessageType.CONTROL:
            # self.control_message_queue.put_nowait(applmessage)

            try:
                self._children.remove(hdr.messagefrom)
                self._child_counter -= 1
            except ValueError as e:
                # print(f"\n\n\n{self.componentinstancenumber}: {e} {hdr.messagefrom} {self._children}\n\n\n")
                pass
        elif hdr.messagetype == ApplicationLayerMessageType.WAVE:
            if applmessage.payload.messagepayload.tag == self.componentinstancenumber:
                self.my_wave_bucket.append(applmessage.payload.messagepayload.response)

                if WAVE_DEBUG:
                    print(f"  WAVE << N-{self.componentinstancenumber} << N-{hdr.messagefrom} ({applmessage.payload.messagepayload.response})")

                if len(self.my_wave_bucket) == (len(self.context["network"].G.nodes()) - 1):
                    if WAVE_DEBUG:
                        print(f"  ::: WAVE END >> N-{self.componentinstancenumber}: {' '.join(['A' if x == SFWaveResponse.ACTIVE else ('F' if x == SFWaveResponse.FINISHED else 'XXX') for x in self.my_wave_bucket])}")

                    if SFWaveResponse.ACTIVE not in self.my_wave_bucket:
                        print(f"  ::: WAVE END >> N-{self.componentinstancenumber}: ANNOUNCE!")
                        self.announce_on_next_tick = True
                    else:
                        print(f"  ::: WAVE END >> N-{self.componentinstancenumber}: NOT DONE YET!")
                        self.my_wave_bucket = []
            else:
                if self.__exited_from_tree:
                    self.send_down(Event(self, EventTypes.MFRT, self.prepare_application_layer_message(ApplicationLayerMessageType.WAVE, applmessage.payload.messagepayload.tag, SFWaveMessagePayload(applmessage.payload.messagepayload.tag, SFWaveMessageType.RESPONSE, SFWaveResponse.FINISHED))))
                    if WAVE_DEBUG:
                        print(f"  WAVE >> N-{self.componentinstancenumber} >> N-{applmessage.payload.messagepayload.tag} >> FINISHED")
                else:
                    self.send_down(Event(self, EventTypes.MFRT, self.prepare_application_layer_message(ApplicationLayerMessageType.WAVE, applmessage.payload.messagepayload.tag, SFWaveMessagePayload(applmessage.payload.messagepayload.tag, SFWaveMessageType.RESPONSE, SFWaveResponse.ACTIVE))))
                    if WAVE_DEBUG:
                        print(f"  WAVE >> N-{self.componentinstancenumber} >> N-{applmessage.payload.messagepayload.tag} >> ACTIVE")

                self._wms += 1
        else:
            print(f"\n!!! N-{self.componentinstancenumber}: GOT MSG UNIDENT: {hdr.messagetype}, FROM {hdr.messagefrom}")
            sys.exit(2)

    def call_wave(self):
        print(f"  > CALL WAVE @@ N-{self.componentinstancenumber}")

        to_nodes = [i for i in range(len(self.context["network"].G.nodes())) if i != self.componentinstancenumber]

        for to in to_nodes:
            self.send_down(Event(self, EventTypes.MFRT, self.prepare_application_layer_message(ApplicationLayerMessageType.WAVE, to, SFWaveMessagePayload(self.componentinstancenumber, SFWaveMessageType.REQUEST))))
            self._wms += 1

    def exit_tree(self):
        # Exit from the tree

        if not self.__exited_from_tree:
            if self._in_tree:
                self._in_tree = False
                if self._parent_node is not None:
                    self.send_ack_control_message(self._parent_node, True)
            
            self._parent_node = None

            if self.componentinstancenumber in self.context["alive_nodes"]:
                self.context["alive_nodes"].remove(self.componentinstancenumber)

            self.__exited_from_tree = True
            print(f"  > EXIT @@ N-{self.componentinstancenumber}")

            self.call_wave()

    def simulation_tick(self):
        next_state = None
        got_packages_from = None
        to_friend = None

        _upd_children = []

        for c in self._children:
            if c in self.context["alive_nodes"]:
                _upd_children.append(c)

        self._children = _upd_children
        self._child_counter = len(self._children)

        if self.announce_on_next_tick:
            __cms =  self._cms
            __wms = self._wms
            self._cms = 0
            self._wms = 0

            return None, None, __cms, __wms

        if self.simulation_state == SFAHCNodeSimulationStatus.OUT_OF_TREE:
            next_state = SFAHCNodeSimulationStatus.OUT_OF_TREE
            # print(f"   ==> N-{self.componentinstancenumber}: OOT")            #NOTE: DEV
            # print(f"   ==> N-{self.componentinstancenumber}: OUT OF TREE")    
        elif self.__tick_n >= self.simulation_ticks_total:
            self.exit_tree()
            next_state = SFAHCNodeSimulationStatus.OUT_OF_TREE
            # print(f"   ==> N-{self.componentinstancenumber}: OOC DEAD")       #NOTE: DEV
        elif self._passive_counter >= self.die_passiveness_threshold:           # NOTE: initiator can also die from passiveness in Shavit-Francez.
            self.exit_tree()
            next_state = SFAHCNodeSimulationStatus.OUT_OF_TREE
            # print(f"   ==> N-{self.componentinstancenumber}: PASSIVE DIE")    #NOTE: DEV
        elif self.__tick_n >= self.hard_stop_on_tick:                           # NOTE: initiator can also hard stop in Shavit-Francez.
            self.exit_tree()
            next_state = SFAHCNodeSimulationStatus.OUT_OF_TREE
            # print(f"   ==> N-{self.componentinstancenumber}: HARD STOP")      #NOTE: DEV
        else:
            if self.simulation_state == SFAHCNodeSimulationStatus.OUT_OF_CLOCK:
                next_state = SFAHCNodeSimulationStatus.OUT_OF_CLOCK
            elif self.simulation_state == SFAHCNodeSimulationStatus.PASSIVE:
                if self.basic_message_queue.empty():
                    if self._in_tree and self._child_counter == 0:
                            self.exit_tree()
                            next_state = SFAHCNodeSimulationStatus.PASSIVE
                            print(f"   ==> N-{self.componentinstancenumber}: OUT OF TREE / PASSIVE")
                    else:
                        # no incoming package, still passive.
                        next_state = SFAHCNodeSimulationStatus.PASSIVE
                else:
                    got_packages_from = []

                    for _ in range(self.package_process_per_tick):
                        try:
                            package = self.basic_message_queue.get_nowait()
                            # print(f"+P+ N-{self.componentinstancenumber} <==BASIC== N-{package.header.messagefrom} ({package.payload.messagepayload})")
                            got_packages_from.append(package.header.messagefrom)
                        except queue.Empty:
                            break

                    self.alive_for_next_ticks = random.randint(self.min_activeness_after_receive, self.max_activeness_after_receive)
                    next_state = SFAHCNodeSimulationStatus.ACTIVE
            elif self.simulation_state == SFAHCNodeSimulationStatus.ACTIVE:
                got_packages_from = []

                for _ in range(self.package_process_per_tick):
                    try:
                        package = self.basic_message_queue.get_nowait()
                        # print(f"+A+ N-{self.componentinstancenumber} <==BASIC== N-{package.header.messagefrom} ({package.payload.messagepayload})")
                        got_packages_from.append(package.header.messagefrom)
                    except queue.Empty:
                        break

                if random.random() <= self.communication_on_active_prob:
                    # send package to a random friend..

                    _alive_ones = [n for n in self.context["alive_nodes"] if n != self.componentinstancenumber]

                    if len(_alive_ones) == 0: # everyone is dead!!!
                        # print(f"  **ROOT** N-{self.componentinstancenumber}: Eveyone is dead!!!")
                        # return None, None       # time to go!
                        to_friend = None
                    else:
                        to_friend = random.choice(_alive_ones)
                        self.send_random_basic_message(to_friend)

                self.alive_for_next_ticks -= 1

                if self.alive_for_next_ticks == 0:
                    if len(got_packages_from) > 0:
                        # got a package, this means immeiate activeness from passive!
                        next_state = SFAHCNodeSimulationStatus.ACTIVE
                        self.alive_for_next_ticks = random.randint(self.min_activeness_after_receive, self.max_activeness_after_receive)
                    else:
                        next_state = SFAHCNodeSimulationStatus.PASSIVE
                else:
                    next_state = SFAHCNodeSimulationStatus.ACTIVE

        assert next_state is not None

        # ST: state
        # NS: next state
        # GPF: got packages from
        # SPF: sent package to friend
        # ANT: alive next ticks
        # P2P: packages to process
        # print(f"   {'ROOT' if self._i_am_root else '==>'} N-{self.componentinstancenumber}: P: {self._parent_node}, CC: ({self._child_counter}) {self._children}, ST: {self.simulation_state}, NS: {next_state}, GPF: {got_packages_from}, SPF: {to_friend}, ANT: {self.alive_for_next_ticks}, P2P: {self.basic_message_queue.qsize()}")

        if self._i_am_root:
            print(f"   {'INIT' if self._i_am_root else '==>'} N-{self.componentinstancenumber}: P: {self._parent_node}, CC: ({self._child_counter}) {self._children}, ST: {self.simulation_state}, NS: {next_state}, GPF: {got_packages_from}, SPF: {to_friend}, ANT: {self.alive_for_next_ticks}, P2P: {self.basic_message_queue.qsize()}")

        # time.sleep(self.sleep_ms_per_tick / 1000)
        self.__tick_n += 1
        self.simulation_state = next_state

        if self.simulation_state == SFAHCNodeSimulationStatus.PASSIVE:
            self._passive_counter += 1
        elif self.simulation_state == SFAHCNodeSimulationStatus.ACTIVE:
            self._passive_counter = 0

        __cms =  self._cms
        __wms = self._wms
        self._cms = 0
        self._wms = 0

        return next_state, to_friend, __cms, __wms

class ShavitFrancezAdHocNode(ComponentModel):
    def __init__(self, componentname, componentid, context):
        self.context = context
        # SUBCOMPONENTS
        self.appllayer = ShavitFrancezApplicationLayerComponent("ApplicationLayer", componentid, context=self.context)
        self.netlayer = AllSeingEyeNetworkLayer("NetworkLayer", componentid)
        self.linklayer = LinkLayer("LinkLayer", componentid)
        # self.failuredetect = GenericFailureDetector("FailureDetector", componentid)

        # CONNECTIONS AMONG SUBCOMPONENTS
        self.appllayer.connect_me_to_component(ConnectorTypes.DOWN, self.netlayer)
        # self.failuredetect.connectMeToComponent(PortNames.DOWN, self.netlayer)
        self.netlayer.connect_me_to_component(ConnectorTypes.UP, self.appllayer)
        # self.netlayer.connectMeToComponent(PortNames.UP, self.failuredetect)
        self.netlayer.connect_me_to_component(ConnectorTypes.DOWN, self.linklayer)
        self.linklayer.connect_me_to_component(ConnectorTypes.UP, self.netlayer)

        # Connect the bottom component to the composite component....
        self.linklayer.connect_me_to_component(ConnectorTypes.DOWN, self)
        self.connect_me_to_component(ConnectorTypes.UP, self.linklayer)

        super().__init__(componentname, componentid, context=self.context)

    def on_init(self, eventobj: Event):
        # print(f"Initializing {self.componentname}.{self.componentinstancenumber}")
        pass

    def on_message_from_top(self, eventobj: Event):
        self.send_down(Event(self, EventTypes.MFRT, eventobj.eventcontent))

    def on_message_from_bottom(self, eventobj: Event):
        self.send_up(Event(self, EventTypes.MFRB, eventobj.eventcontent))

    def simulation_tick(self):
        return self.appllayer.simulation_tick()

    @property
    def waiting_packages_on_queue(self):
        if self.appllayer.simulation_state == SFAHCNodeSimulationStatus.OUT_OF_CLOCK or self.appllayer.simulation_state == SFAHCNodeSimulationStatus.OUT_OF_TREE:
            return 0

        return self.appllayer.basic_message_queue.qsize()

    @property
    def parent_node(self):
        return self.appllayer._parent_node