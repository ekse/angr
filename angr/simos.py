"""
Manage OS-level configuration
"""

import logging
l = logging.getLogger("angr.simos")

from archinfo import ArchARM, ArchMIPS32, ArchX86, ArchAMD64
from simuvex import SimState, SimIRSB, SimStateSystem, SimActionData
from simuvex import s_options as o
from simuvex.s_procedure import SimProcedure, SimProcedureContinuation
from cle import MetaELF, BackedCGC
import pyvex

class SimOS(object):
    """A class describing OS/arch-level configuration"""

    def __init__(self, project):
        self.arch = project.arch
        self.proj = project
        self.continue_addr = None

    def configure_project(self):
        """Configure the project to set up global settings (like SimProcedures)"""
        self.continue_addr = self.proj._extern_obj.get_pseudo_addr('angr##simproc_continue')
        self.proj.hook(self.continue_addr, SimProcedureContinuation)

        def irelative_resolver(resolver_addr):
            resolver = self.proj.factory.callable(resolver_addr, concrete_only=True)
            try:
                val = resolver()
            except AngrCallableError:
                l.error("Resolver at %#x failed to resolve!", resolver_addr)
                return None

            if not val.singlevalued:
                l.error("Resolver at %#x failed to resolve! (multivalued)", resolver_addr)
                return None

            return val._model_concrete.value

        self.proj.loader.perform_irelative_relocs(irelative_resolver)

    def state_blank(self, addr=None, initial_prefix=None, **kwargs):
        if kwargs.get('mode', None) is None:
            kwargs['mode'] = self.proj._default_analysis_mode
        if kwargs.get('permissions_backer', None) is None:
            # just a dict of address ranges to permission bits
            permission_map = { }
            for obj in self.proj.loader.all_objects:
                for seg in obj.segments:
                    perms = 0
                    # bit values based off of protection bit values from sys/mman.h
                    if seg.is_readable:
                        perms |= 1 # PROT_READ
                    if seg.is_writable:
                        perms |= 2 # PROT_WRITE
                    if seg.is_executable:
                        perms |= 4 # PROT_EXEC
                    permission_map[(obj.rebase_addr + seg.min_addr, obj.rebase_addr + seg.max_addr)] = perms
            permissions_backer = (self.proj.loader.main_bin.execstack, permission_map)
            kwargs['permissions_backer'] = permissions_backer
        if kwargs.get('memory_backer', None) is None:
            kwargs['memory_backer'] = self.proj.loader.memory
        if kwargs.get('arch', None) is None:
            kwargs['arch'] = self.proj.arch

        state = SimState(**kwargs)
        state.regs.sp = self.arch.initial_sp

        if initial_prefix is not None:
            for reg in state.arch.default_symbolic_registers:
                state.registers.store(reg, state.se.Unconstrained(initial_prefix + "_" + reg,
                                                            state.arch.bits,
                                                            explicit_name=True))

        for reg, val, is_addr, mem_region in state.arch.default_register_values:
            if o.ABSTRACT_MEMORY in state.options and is_addr:
                address = state.se.ValueSet(region=mem_region, bits=state.arch.bits, val=val)
                state.registers.store(reg, address)
            else:
                state.registers.store(reg, val)

        if addr is None: addr = self.proj.entry
        state.regs.ip = addr

        state.scratch.ins_addr = addr
        state.scratch.bbl_addr = addr
        state.scratch.stmt_idx = 0
        state.scratch.jumpkind = 'Ijk_Boring'

        state.procedure_data.hook_addr = self.continue_addr
        return state

    def state_entry(self, **kwargs):
        return self.state_blank(**kwargs)

    def state_full_init(self, **kwargs):
        return self.state_entry(**kwargs)

    def prepare_call_state(self, calling_state, initial_state=None,
                           preserve_registers=(), preserve_memory=()):
        '''
        This function prepares a state that is executing a call instruction.
        If given an initial_state, it copies over all of the critical registers to it from the
        calling_state. Otherwise, it prepares the calling_state for action.

        This is mostly used to create minimalistic for CFG generation. Some ABIs, such as MIPS PIE and
        x86 PIE, require certain information to be maintained in certain registers. For example, for
        PIE MIPS, this function transfer t9, gp, and ra to the new state.
        '''

        if isinstance(self.arch, ArchMIPS32):
            if initial_state is not None:
                initial_state = self.state_blank()
            mips_caller_saves = ('s0', 's1', 's2', 's3', 's4', 's5', 's6', 's7', 'gp', 'sp', 'bp', 'ra')
            preserve_registers = preserve_registers + mips_caller_saves + ('t9',)

        if initial_state is None:
            new_state = calling_state.copy()
        else:
            new_state = initial_state.copy()
            for reg in set(preserve_registers):
                new_state.registers.store(reg, calling_state.registers.load(reg))
            for addr, val in set(preserve_memory):
                new_state.memory.store(addr, calling_state.memory.load(addr, val))

        return new_state

    def prepare_function_symbol(self, symbol_name):
        '''
        Prepare the address space with the data necessary to perform relocations pointing to the given symbol
        '''
        return self.proj._extern_obj.get_pseudo_addr(symbol_name)

class SimLinux(SimOS):
    """OS-specific configuration for *nix-y OSes"""
    def __init__(self, *args, **kwargs):
        super(SimLinux, self).__init__(*args, **kwargs)

        self._loader_addr = None
        self._loader_lock_addr = None
        self._loader_unlock_addr = None
        self._vsyscall_addr = None

    def configure_project(self):
        super(SimLinux, self).configure_project()

        self._loader_addr = self.proj._extern_obj.get_pseudo_addr('angr##loader')
        self._loader_lock_addr = self.proj._extern_obj.get_pseudo_addr('angr##loader_lock')
        self._loader_unlock_addr = self.proj._extern_obj.get_pseudo_addr('angr##loader_unlock')
        self._vsyscall_addr = self.proj._extern_obj.get_pseudo_addr('angr##vsyscall')
        self.proj.hook(self._loader_addr, LinuxLoader, kwargs={'project': self.proj})
        self.proj.hook(self._loader_lock_addr, _dl_rtld_lock_recursive)
        self.proj.hook(self._loader_unlock_addr, _dl_rtld_unlock_recursive)
        self.proj.hook(self._vsyscall_addr, _vsyscall)

        ld_obj = self.proj.loader.linux_loader_object
        if ld_obj is not None:
            tlsfunc = ld_obj.get_symbol('__tls_get_addr')
            if tlsfunc is not None:
                self.proj.hook(tlsfunc.rebased_addr, _tls_get_addr, kwargs={'ld': self.proj.loader})
            tlsfunc2 = ld_obj.get_symbol('___tls_get_addr')
            if tlsfunc2 is not None:
                if self.proj.arch.name == 'X86':
                    self.proj.hook(tlsfunc2.rebased_addr, _tls_get_addr_tunder_x86, kwargs={'ld': self.proj.loader})
                else:
                    l.warning("Found an unknown ___tls_get_addr, please tell Andrew")

            _rtld_global = ld_obj.get_symbol('_rtld_global')
            if _rtld_global is not None:
                if isinstance(self.proj.arch, ArchAMD64):
                    self.proj.loader.memory.write_addr_at(_rtld_global.rebased_addr + 0xF08, self._loader_lock_addr)
                    self.proj.loader.memory.write_addr_at(_rtld_global.rebased_addr + 0xF10, self._loader_unlock_addr)

            _rtld_global_ro = ld_obj.get_symbol('_rtld_global_ro')
            if _rtld_global_ro is not None:
                pass

        tls_obj = self.proj.loader.tls_object
        if tls_obj is not None:
            if isinstance(self.proj.arch, ArchAMD64):
                self.proj.loader.memory.write_addr_at(tls_obj.thread_pointer + 0x28, 0x5f43414e4152595f)
                self.proj.loader.memory.write_addr_at(tls_obj.thread_pointer + 0x30, 0x5054524755415244)
            elif isinstance(self.proj.arch, ArchX86):
                self.proj.loader.memory.write_addr_at(tls_obj.thread_pointer + 0x10, self._vsyscall_addr)
            elif isinstance(self.proj.arch, ArchARM):
                self.proj.hook(0xffff0fe0, _kernel_user_helper_get_tls, kwargs={'ld': self.proj.loader})


        # Only set up ifunc resolution if we are using the ELF backend on AMD64
        if isinstance(self.proj.loader.main_bin, MetaELF):
            if isinstance(self.proj.arch, ArchAMD64):
                for binary in self.proj.loader.all_objects:
                    if not isinstance(binary, MetaELF):
                        continue
                    for reloc in binary.relocs:
                        if reloc.symbol is None or reloc.resolvedby is None:
                            continue
                        if reloc.resolvedby.type != 'STT_GNU_IFUNC':
                            continue
                        gotaddr = reloc.addr + binary.rebase_addr
                        gotvalue = self.proj.loader.memory.read_addr_at(gotaddr)
                        if self.proj.is_hooked(gotvalue):
                            continue
                        # Replace it with a ifunc-resolve simprocedure!
                        kwargs = {
                                'proj': self.proj,
                                'funcaddr': gotvalue,
                                'gotaddr': gotaddr,
                                'funcname': reloc.symbol.name
                        }
                        randaddr = self.proj._extern_obj.get_pseudo_addr('ifunc_' + reloc.symbol.name)
                        self.proj.hook(randaddr, IFuncResolver, kwargs=kwargs)
                        self.proj.loader.memory.write_addr_at(gotaddr, randaddr)

    def state_blank(self, fs=None, concrete_fs=False, chroot=None, **kwargs):
        state = super(SimLinux, self).state_blank(**kwargs) #pylint:disable=invalid-name

        if self.proj.loader.tls_object is not None:
            if isinstance(state.arch, ArchAMD64):
                state.regs.fs = self.proj.loader.tls_object.thread_pointer
            elif isinstance(state.arch, ArchX86):
                state.regs.gs = self.proj.loader.tls_object.thread_pointer >> 16
            elif isinstance(state.arch, ArchMIPS32):
                state.regs.ulr = self.proj.loader.tls_object.thread_pointer

        state.register_plugin('posix', SimStateSystem(fs=fs, concrete_fs=concrete_fs, chroot=chroot))

        if self.proj.loader.main_bin.is_ppc64_abiv1:
            state.libc.ppc64_abiv = 'ppc64_1'

        return state

    def state_entry(self, args=None, env=None, sargc=None, **kwargs):
        state = super(SimLinux, self).state_entry(**kwargs)

        # Handle default values
        if args is None:
            args = []

        if env is None:
            env = {}

        # Prepare argc
        argc = state.se.BVV(len(args), state.arch.bits)
        if sargc is not None:
            argc = state.se.Unconstrained("argc", state.arch.bits)

        # Make string table for args/env/auxv
        table = StringTableSpec()

        # Add args to string table
        for arg in args:
            table.add_string(arg)
        table.add_null()

        # Add environment to string table
        for k, v in env.iteritems():
            table.add_string(k + '=' + v)
        table.add_null()

        # Prepare the auxiliary vector and add it to the end of the string table
        # TODO: Actually construct a real auxiliary vector
        aux = []
        for a, b in aux:
            table.add_pointer(a)
            table.add_pointer(b)
        table.add_null()
        table.add_null()

        # Dump the table onto the stack, calculate pointers to args, env, and auxv
        state.memory.store(state.regs.sp, state.se.BVV(0, 8*16), endness='Iend_BE')
        argv = table.dump(state, state.regs.sp)
        envp = argv + ((len(args) + 1) * state.arch.bytes)
        auxv = argv + ((len(args) + len(env) + 2) * state.arch.bytes)

        # Put argc on stack and fix the stack pointer
        newsp = argv - state.arch.bytes
        state.memory.store(newsp, argc, endness=state.arch.memory_endness)
        state.regs.sp = newsp

        if state.arch.name in ('PPC32',):
            state.stack_push(state.se.BVV(0, 32))
            state.stack_push(state.se.BVV(0, 32))
            state.stack_push(state.se.BVV(0, 32))
            state.stack_push(state.se.BVV(0, 32))

        # store argc argv envp auxv in the posix plugin
        state.posix.argv = argv
        state.posix.argc = argc
        state.posix.environ = envp
        state.posix.auxv = auxv
        self.set_entry_register_values(state)

        return state

    def set_entry_register_values(self, state):
        for reg, val in state.arch.entry_register_values.iteritems():
            if isinstance(val, (int, long)):
                state.registers.store(reg, val, size=state.arch.bytes)
            elif isinstance(val, (str,)):
                if val == 'argc':
                    state.registers.store(reg, state.posix.argc, size=state.arch.bytes)
                elif val == 'argv':
                    state.registers.store(reg, state.posix.argv)
                elif val == 'envp':
                    state.registers.store(reg, state.posix.environ)
                elif val == 'auxv':
                    state.registers.store(reg, state.posix.auxv)
                elif val == 'ld_destructor':
                    # a pointer to the dynamic linker's destructor routine, to be called at exit
                    # or NULL. We like NULL. It makes things easier.
                    state.registers.store(reg, state.se.BVV(0, state.arch.bits))
                elif val == 'toc':
                    if self.proj.loader.main_bin.is_ppc64_abiv1:
                        state.registers.store(reg, self.proj.loader.main_bin.ppc64_initial_rtoc)
                else:
                    l.warning('Unknown entry point register value indicator "%s"', val)
            else:
                l.error('What the ass kind of default value is %s?', val)

    def state_full_init(self, **kwargs):
        kwargs['addr'] = self.proj._extern_obj.get_pseudo_addr('angr##loader')
        return super(SimLinux, self).state_full_init(**kwargs)

    def prepare_function_symbol(self, symbol_name):
        '''
        Prepare the address space with the data necessary to perform relocations pointing to the given symbol
        '''
        if self.arch.name == 'PPC64':
            pseudo_hookaddr = self.proj._extern_obj.get_pseudo_addr(symbol_name + '#func')
            pseudo_toc = self.proj._extern_obj.get_pseudo_addr(symbol_name + '#func', size=0x18)
            self.proj._extern_obj.memory.write_addr_at(pseudo_toc - self.proj._extern_obj.rebase_addr, pseudo_hookaddr)
            return pseudo_hookaddr
        else:
            return self.proj._extern_obj.get_pseudo_addr(symbol_name)

class SimCGC(SimOS):
    def state_blank(self, fs=None, **kwargs):

        # Set CGC-specific options
        # In this way those options can still be removed by "remove_options" argument
        all_options = set()
        if 'options' in kwargs:
            all_options |= kwargs['options']
        if 'add_options' in kwargs:
            all_options |= kwargs['add_options']
        if o.CGC_ZERO_FILL_UNCONSTRAINED_MEMORY not in all_options:
            # s.options.add(o.CGC_NO_SYMBOLIC_RECEIVE_LENGTH)
            kwargs['add_options'] = kwargs['add_options'] if 'add_options' in kwargs else set()
            kwargs['add_options'].add(o.CGC_ZERO_FILL_UNCONSTRAINED_MEMORY)

        s = super(SimCGC, self).state_blank(**kwargs)  # pylint:disable=invalid-name

        # Special stack base for CGC binaries to work with Shellphish CRS
        s.regs.sp = 0xbaff0000

        s.register_plugin('posix', SimStateSystem(fs=fs))

        # Create the CGC plugin
        s.get_plugin('cgc')

        return s

    def state_entry(self, **kwargs):
        if isinstance(self.proj.loader.main_bin, BackedCGC):
            kwargs['permissions_backer'] = (True, self.proj.loader.main_bin.permissions_map)

        state = super(SimCGC, self).state_entry(**kwargs)

        if isinstance(self.proj.loader.main_bin, BackedCGC):
            for reg, val in self.proj.loader.main_bin.initial_register_values():
                if reg in state.arch.registers:
                    setattr(state.regs, reg, val)
                elif reg == 'eflags':
                    pass
                elif reg == 'fctrl':
                    state.regs.fpround = (val & 0xC00) >> 10
                elif reg == 'fstat':
                    state.regs.fc3210 = (val & 0x4700)
                elif reg == 'ftag':
                    empty_bools = [((val >> (x*2)) & 3) == 3 for x in xrange(8)]
                    tag_chars = [state.se.BVV(0 if x else 1, 8) for x in empty_bools]
                    for i, tag in enumerate(tag_chars):
                        setattr(state.regs, 'fpu_t%d' % i, tag)
                elif reg in ('fiseg', 'fioff', 'foseg', 'fooff', 'fop'):
                    pass
                elif reg == 'mxcsr':
                    state.regs.sseround = (val & 0x600) >> 9
                else:
                    l.error("What is this register %s I have to translate?", reg)

            # Update allocation base
            state.cgc.allocation_base = self.proj.loader.main_bin.current_allocation_base

            # Do all the writes
            writes_backer = self.proj.loader.main_bin.writes_backer
            stdout = 1
            for size in writes_backer:
                if size == 0:
                    continue
                str_to_write = state.posix.files[1].content.load(state.posix.files[1].pos, size)
                a = SimActionData(state, 'file_1_0', 'write', addr=state.se.BVV(state.posix.files[1].pos, state.arch.bits), data=str_to_write, size=size)
                state.posix.write(stdout, str_to_write, size)
                state.log.add_action(a)

        else:
            # Set CGC-specific variables
            state.regs.eax = 0
            state.regs.ebx = 0
            state.regs.ecx = 0
            state.regs.edx = 0
            state.regs.edi = 0
            state.regs.esi = 0
            state.regs.esp = 0xbaaaaffc
            state.regs.ebp = 0
            #state.regs.eflags = s.se.BVV(0x202, 32)

            # fpu values
            state.regs.mm0 = 0
            state.regs.mm1 = 0
            state.regs.mm2 = 0
            state.regs.mm3 = 0
            state.regs.mm4 = 0
            state.regs.mm5 = 0
            state.regs.mm6 = 0
            state.regs.mm7 = 0
            state.regs.fpu_tags = 0
            state.regs.fpround = 0
            state.regs.fc3210 = 0x0300
            state.regs.ftop = 0

            # sse values
            state.regs.sseround = 0
            state.regs.xmm0 = 0
            state.regs.xmm1 = 0
            state.regs.xmm2 = 0
            state.regs.xmm3 = 0
            state.regs.xmm4 = 0
            state.regs.xmm5 = 0
            state.regs.xmm6 = 0
            state.regs.xmm7 = 0

        return state

#
# Loader-related simprocedures
#

class IFuncResolver(SimProcedure):
    NO_RET = True

    # pylint: disable=arguments-differ,unused-argument
    def run(self, proj=None, funcaddr=None, gotaddr=None, funcname=None):
        resolve = proj.factory.callable(funcaddr, concrete_only=True)
        try:
            value = resolve()
        except AngrCallableError:
            l.critical("Ifunc \"%s\" failed to resolve!", funcname)
            #import IPython; IPython.embed()
            raise
        self.state.memory.store(gotaddr, value, endness=self.state.arch.memory_endness)
        self.add_successor(self.state, value, self.state.se.true, 'Ijk_Boring')

    def __repr__(self):
        return '<IFuncResolver %s>' % self.kwargs.get('funcname', None)

class LinuxLoader(SimProcedure):
    NO_RET = True

    # pylint: disable=unused-argument,arguments-differ,attribute-defined-outside-init
    local_vars = ('initializers',)
    def run(self, project=None):
        self.initializers = project.loader.get_initializers()
        self.run_initializer(project)

    def run_initializer(self, project=None):
        if len(self.initializers) == 0:
            project._simos.set_entry_register_values(self.state)
            self.jump(project.entry)
        else:
            addr = self.initializers.pop(0)
            self.call(addr, (self.state.posix.argc, self.state.posix.argv, self.state.posix.environ), 'run_initializer')

class _tls_get_addr(SimProcedure):
    # pylint: disable=arguments-differ
    def run(self, ptr, ld=None):
        module_id = self.state.se.any_int(self.state.memory.load(ptr, self.state.arch.bytes, endness=self.state.arch.memory_endness))
        offset = self.state.se.any_int(self.state.memory.load(ptr+self.state.arch.bytes, self.state.arch.bytes, endness=self.state.arch.memory_endness))
        return self.state.se.BVV(ld.tls_object.get_addr(module_id, offset), self.state.arch.bits)

class _tls_get_addr_tunder_x86(SimProcedure):
    # pylint: disable=arguments-differ
    def run(self, ld=None):
        ptr = self.state.regs.eax
        return self.inline_call(_tls_get_addr, ptr, ld=ld).ret_expr

class _dl_rtld_lock_recursive(SimProcedure):
    # pylint: disable=arguments-differ, unused-argument
    def run(self, lock):
        # For future reference:
        # ++((pthread_mutex_t *)(lock))->__data.__count;
        return

class _dl_rtld_unlock_recursive(SimProcedure):
    def run(self):
        return

class _vsyscall(SimProcedure):
    NO_RET = True

    # This is pretty much entirely copied from SimProcedure.ret
    def run(self):
        if self.cleanup:
            self.state.options.discard(o.AST_DEPS)
            self.state.options.discard(o.AUTO_REFS)

        ret_irsb = pyvex.IRSB(arch=self.state.arch, bytes=self.state.arch.ret_instruction, mem_addr=self.addr)
        ret_simirsb = SimIRSB(self.state, ret_irsb, inline=True, addr=self.addr)
        if not ret_simirsb.flat_successors + ret_simirsb.unsat_successors:
            ret_state = ret_simirsb.default_exit
        else:
            ret_state = (ret_simirsb.flat_successors + ret_simirsb.unsat_successors)[0]

        if self.cleanup:
            self.state.options.add(o.AST_DEPS)
            self.state.options.add(o.AUTO_REFS)

        self.add_successor(ret_state, ret_state.scratch.target, ret_state.scratch.guard, 'Ijk_Sys')

class _kernel_user_helper_get_tls(SimProcedure):
    # pylint: disable=arguments-differ
    def run(self, ld=None):
        self.state.regs.r0 = ld.tls_object.thread_pointer
        return

os_mapping = {
    'unix': SimLinux,
    'unknown': SimOS,
    'windows': SimOS,
    'cgc': SimCGC
}

from .errors import AngrCallableError
from .tablespecs import StringTableSpec
