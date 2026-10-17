import unittest
from types import SimpleNamespace
import mido
from partykeys_bridge.routing import discover_boards, BoardWatcher

class RoutingTests(unittest.TestCase):
    def run_discovery(self,pending):
        clock=[0.0];sent=[]
        source=SimpleNamespace(iter_pending=lambda:pending.pop(0) if pending else [])
        result=discover_boards(source,SimpleNamespace(send=sent.append),
              now=lambda:clock[0],sleep=lambda t:clock.__setitem__(0,clock[0]+t))
        return result,sent
    def report(self,ch,addr,value):
        return mido.Message('sysex',data=(5,0x30,0x7e,0x7e,ch,addr,2,value,9))
    def test_discovers_joined_routes_and_assigns_only_unassigned_main(self):
        result,sent=self.run_discovery([[self.report(0,0x40,0),self.report(0,0x3f,1)],
                                      [self.report(1,0x3f,1),self.report(1,0x40,0)]])
        self.assertEqual(result, ([(1,0x3f,48),(1,0x40,84)],{}))
        self.assertEqual(len(sent),2)
        self.assertEqual(sent[1].data,(5,0x30,0,0x7f,0x20,0,3,1))
    def test_existing_channels_are_not_reassigned(self):
        result,sent=self.run_discovery([[self.report(3,0x3f,1),self.report(3,0x40,0)]])
        self.assertEqual(result[0],[(3,0x3f,48),(3,0x40,84)])
        self.assertEqual(len(sent),1)
    def test_no_reply_fails_without_lighting_broadcast(self):
        with self.assertRaises(OSError):self.run_discovery([])


class BoardWatcherTests(unittest.TestCase):
    def setUp(self):
        self.clock=[0.0];self.sent=[]
        def send(message):
            self.sent.append((self.clock[0],message));self.driver.last_send=self.clock[0]
        self.driver=SimpleNamespace(last_send=-100,min_interval=.25,_send=send)
        self.main=(1,0x3f,48);self.sub=(1,0x40,84)
        self.w=BoardWatcher([self.main,self.sub],now=lambda:self.clock[0])
    def report(self,channel,address,command,value=0):
        return mido.Message('sysex',data=(5,0x30,0x7e,0x7e,channel,address,command,value,9))
    def query(self):
        self.clock[0]=self.w.query_at
        self.assertIsNone(self.w.tick(self.driver))
    def finish(self):
        self.clock[0]=self.w.deadline
        return self.w.tick(self.driver)
    def test_remove_is_debounced_and_replaces_complete_snapshot(self):
        self.assertTrue(self.w.receive(self.report(1,0x40,5)))
        self.assertTrue(self.w.suspended)
        self.clock[0]=.5;self.w.receive(self.report(1,0x40,5))
        self.assertAlmostEqual(self.w.query_at,1.2)
        self.query();self.w.receive(self.report(1,0x3f,2,1))
        self.assertEqual(self.finish(),([self.main],{},True))
        self.assertFalse(self.w.suspended)
    def test_add_and_pacing_do_not_sleep_or_clear_other_messages(self):
        self.w=BoardWatcher([self.main],now=lambda:self.clock[0])
        self.w.receive(self.report(1,0x40,4))
        self.clock[0]=self.w.query_at;self.driver.last_send=self.clock[0]
        self.w.tick(self.driver);self.assertEqual(self.sent,[])
        self.clock[0]+=.25;self.w.tick(self.driver)
        self.w.receive(mido.Message('note_on',note=60))
        self.w.receive(self.report(1,0x3f,2,1));self.w.receive(self.report(1,0x40,2))
        self.assertEqual(self.finish(),([self.main,self.sub],{},True))
    def test_periodic_removal_recovery_and_silent_query_retains_known_routes(self):
        self.query();self.w.receive(self.report(1,0x3f,2,1))
        self.assertEqual(self.finish(),([self.main],{},False))
        self.query();self.assertIsNone(self.finish())
        self.assertEqual(self.w.boards,[self.main]);self.assertFalse(self.w.suspended)
        self.w.receive(self.report(1,0x3f,5));self.query()
        with self.assertLogs('partykeys',level='WARNING'):self.assertIsNone(self.finish())
        self.assertTrue(self.w.suspended)
        self.query();self.w.receive(self.report(1,0x3f,2,1))
        self.assertIsNotNone(self.finish());self.assertFalse(self.w.suspended)
    def test_unassigned_main_assigned_once_and_reinitializes_after_ack(self):
        self.w.receive(self.report(0,0x3f,4));self.query()
        msg=self.report(0,0x3f,2,1)
        self.w.receive(msg);self.clock[0]+=.25;self.w.tick(self.driver)
        self.assertEqual(self.sent[-1][1].data[-2:],(3,2))
        count=len(self.sent);self.w.receive(msg);self.clock[0]+=.25;self.w.tick(self.driver)
        self.assertEqual(len(self.sent),count)
        self.w.receive(self.report(2,0x3f,2,1))
        self.assertEqual(self.finish(),([(2,0x3f,48)],{},True))
