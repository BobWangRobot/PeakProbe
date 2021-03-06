from __future__ import division
import sys,copy,os
import numpy as np
#CCTBX IMPORTS
import iotbx.pdb
from mmtbx import monomer_library
from cctbx import miller
from cctbx import crystal
from cctbx import xray
from scitbx.array_family import flex
import mmtbx.utils
from iotbx import reflection_file_utils
from mmtbx.command_line import geometry_minimization
from mmtbx.geometry_restraints.reference import add_coordinate_restraints
from mmtbx import monomer_library
import mmtbx.refinement.geometry_minimization
from cctbx import maptbx
from iotbx.ccp4_map import write_ccp4_map
from cctbx import sgtbx
from cctbx import uctbx
from cctbx import geometry_restraints as cgr
import mmtbx.utils
from mmtbx.refinement import print_statistics
from iotbx import reflection_file_utils
from cctbx.array_family import flex
from cctbx import maptbx
#PProbe imports
from PProbe_util import Util as pputil

null_log = open(os.devnull,'w')


#our class gets passed the following:
#pdb root = pdb file we're working with
#map_coeffs = filename of MTZ weighted 2fofc and fofc map coeffs
#symmetry = cctbx symmetry object of original structure (sg, unit cell)
# 3 different pdb hierarchies:
#   1) original structure with all atoms/waters/etc. "orig_pdb"
#   2) structure stripped of water/sulfate/phospahte "strip_pdb"
#   3) pdb format of fofc peaks from #2 "peaks_pdb"


class StructData:
    def __init__(self,pdb_code,symmetry,orig_pdb_hier,strip_pdb_hier,peak_pdb_hier,map_file,resolution):
        #instantiate utility class
        self.pput = pputil()
        self.write_atom = self.pput.write_atom
        self.pdb_code = pdb_code
        self.orig_symmetry = symmetry
        self.orig_pdb_hier = orig_pdb_hier 
        self.orig_xrs = self.orig_pdb_hier.extract_xray_structure(crystal_symmetry=self.orig_symmetry)
        self.strip_pdb_hier = strip_pdb_hier 
        self.peak_pdb_hier = peak_pdb_hier 
        self.map_file = map_file
        #for gridding / setup in std setting
        self.bound = 5.0
        
        #make all pdb, asu maps, and restraints upon construction
        self.fofcsig = self.make_fofc_map() #also makes map data
        self.twofofcsig = self.make_2fofc_map() #also makes map data
        self.std_so4_pdb,self.std_so4_hier,self.std_so4_xrs = self.place_so4()
        self.std_wat_pdb,self.std_wat_hier,self.std_wat_xrs = self.place_wat()
        self.so4_restraints_1sig = self.make_so4_restraints(0.5)
        self.so4_restraints_01sig = self.make_so4_restraints(0.05)
        self.wat_restraints_1sig = self.make_wat_restraints(1.5)
        self.wat_restraints_01sig = self.make_wat_restraints(0.15)
        self.resolution = resolution
        self.res_bin = self.pput.assign_bin(resolution)
        self.solvent_content = self.get_solvent_content()
        #masks are for map operations later
        self.dist_mat = self.make_dist_mask()
        self.round_mask = self.make_round_mask(self.dist_mat,2.0)#fixed radius of 2.0A
        self.shaped_mask = self.make_shaped_mask(self.dist_mat)


    def get_solvent_content(self):
        masks_obj = mmtbx.masks.bulk_solvent(self.orig_xrs,True,solvent_radius = 1.1,shrink_truncation_radius=1.0,grid_step=0.25)
        return masks_obj.contact_surface_fraction

    def make_fofc_map(self):
        # FOFC map of the whole original asu
        map_coeff = reflection_file_utils.extract_miller_array_from_file(
            file_name = self.map_file,
            label     = "FOFCWT,PHFOFCWT",
            type      = "complex",
            log       = null_log)
        map_sym = map_coeff.crystal_symmetry()
        fft_map = map_coeff.fft_map(resolution_factor=0.25)
        mapsig = np.nanstd(fft_map.real_map_unpadded().as_numpy_array())
        fft_map.apply_sigma_scaling()
        self.fofc_map_data = fft_map.real_map_unpadded()
        return mapsig

    def make_2fofc_map(self):
        # 2FOFC map of the whole original asu
        map_coeff = reflection_file_utils.extract_miller_array_from_file(
            file_name = self.map_file,
            label     = "2FOFCWT,PH2FOFCWT",
            type      = "complex",
            log       = null_log)
        map_sym= map_coeff.crystal_symmetry()
        fft_map = map_coeff.fft_map(resolution_factor=0.25)
        mapsig = np.nanstd(fft_map.real_map_unpadded().as_numpy_array())
        fft_map.apply_sigma_scaling()
        self.twofofc_map_data = fft_map.real_map_unpadded()
        return mapsig
    
    #create a generic SO4 restraints object, re-used for every refinement

    def make_so4_restraints(self,sigma):
        #somehow atom orders get swapped between the pdb.input object
        #and the hier object, causing disaster
        raw_records=self.std_so4_hier.as_pdb_string()
        processed_pdb = monomer_library.pdb_interpretation.process(
            mon_lib_srv               = monomer_library.server.server(),
            ener_lib                  = monomer_library.server.ener_lib(),
            file_name                 = None,
            raw_records               = raw_records,
            crystal_symmetry          = self.std_so4_pdb.crystal_symmetry(),
            force_symmetry            = True)
        geometry = processed_pdb.geometry_restraints_manager(
            show_energies      = False,
            plain_pairs_radius = 5.0)
        chain_proxy=processed_pdb.all_chain_proxies
        sites_start = self.std_so4_xrs.sites_cart()
        selection=chain_proxy.selection("Element S")
        isel = selection.iselection()
        harm_proxy = add_coordinate_restraints(
            sites_cart=sites_start.select(isel),
            selection=isel,
            sigma=sigma)
        restraints_manager = mmtbx.restraints.manager(geometry=geometry,
                                                      normalization = False)
        restraints_manager.geometry.reference_coordinate_proxies = harm_proxy
        return restraints_manager

    def make_wat_restraints(self,sigma):
        raw_records=self.std_wat_pdb.as_pdb_string()
        processed_pdb = monomer_library.pdb_interpretation.process(
            mon_lib_srv               = monomer_library.server.server(),
            ener_lib                  = monomer_library.server.ener_lib(),
            file_name                 = None,
            raw_records               = raw_records,
            crystal_symmetry          = self.std_wat_pdb.crystal_symmetry(),
            force_symmetry            = True)

        geometry = processed_pdb.geometry_restraints_manager(
            show_energies      = False,
            plain_pairs_radius = 5.0)
        chain_proxy=processed_pdb.all_chain_proxies
        sites_start = self.std_wat_xrs.sites_cart()
        selection=chain_proxy.selection("Element O")
        isel = selection.iselection()
        harm_proxy = add_coordinate_restraints(
            sites_cart=sites_start.select(isel),
            selection=isel,
            sigma=sigma)

        restraints_manager = mmtbx.restraints.manager(geometry      = geometry,
                                                      normalization = False)
        restraints_manager.geometry.reference_coordinate_proxies = harm_proxy
        return restraints_manager


    """
    functions for map masks on the standard grid, compute once for every structure
    """
    def make_dist_mask(self):
        ref_map_grid = self.pput.new_grid((0.0,0.0,0.0),5.0)
        dist_grid = np.zeros(ref_map_grid.shape)
        return np.apply_along_axis(np.linalg.norm,1,ref_map_grid)

    def make_round_mask(self,dist_mat,radius):
        mask = np.less_equal(dist_mat,np.ones(dist_mat.shape)*float(radius))
        return np.array(mask,dtype=np.int)

    def make_shaped_mask(self,dist_mat):
        shape_func = lambda x: np.exp(-(0.5*(np.clip(x-0.1,0,np.inf)))**6.0)
        shaped_mask = np.apply_along_axis(shape_func,0,dist_mat)
        return shaped_mask

                         

    """
    Place sulfate and water in standard settings
    """
    
    def place_so4(self,b_fac=35.0,occ=1.00):
        #places a sulfate at the origin of a 10A cubic P1 cell

        pdb_string="CRYST1%9.3f%9.3f%9.3f  90.00  90.00  90.00 P 1            1\n" % \
            (2.0*self.bound,2.0*self.bound,2.0*self.bound)
        x1,y1,z1=5.0,5.0,5.0
        x2,y2,z2 = x1+0.873,y1+0.873,z1+0.873
        x3,y3,z3 = x1-0.873,y1-0.873,z1+0.873
        x4,y4,z4 = x1-0.873,y1+0.873,z1-0.873
        x5,y5,z5 = x1+0.873,y1-0.873,z1-0.873
        so4_dict = {"sx":x1,"sy":y1,"sz":z1,
                    "o1x":x2,"o1y":y2,"o1z":z2,
                    "o2x":x3,"o2y":y3,"o2z":z3,
                    "o3x":x4,"o3y":y4,"o3z":z4,
                    "o4x":x5,"o4y":y5,"o4z":z5}
        pdb_entry = ""
        pdb_entry=pdb_entry+self.write_atom(1,"S","","SO4","X",1,"",so4_dict['sx'],
                                            so4_dict['sy'],so4_dict['sz'],occ,b_fac,"S","")
        pdb_entry=pdb_entry+self.write_atom(2,"O1","","SO4","X",1,"",so4_dict['o1x'],
                                            so4_dict['o1y'],so4_dict['o1z'],occ,b_fac,"O","")
        pdb_entry=pdb_entry+self.write_atom(3,"O2","","SO4","X",1,"",so4_dict['o2x'],
                                            so4_dict['o2y'],so4_dict['o2z'],occ,b_fac,"O","")
        pdb_entry=pdb_entry+self.write_atom(4,"O3","","SO4","X",1,"",so4_dict['o3x'],
                                            so4_dict['o3y'],so4_dict['o3z'],occ,b_fac,"O","")
        pdb_entry=pdb_entry+self.write_atom(5,"O4","","SO4","X",1,"",so4_dict['o4x'],
                                            so4_dict['o4y'],so4_dict['o4z'],occ,b_fac,"O","")
        sulfate_pdb = pdb_string+pdb_entry
        std_so4_pdb = iotbx.pdb.input(source_info=None, lines=flex.split_lines(sulfate_pdb))
        local_sym = std_so4_pdb.crystal_symmetry()
        std_so4_hier = std_so4_pdb.construct_hierarchy(set_atom_i_seq=True)
        std_so4_xrs = std_so4_hier.extract_xray_structure(crystal_symmetry = local_sym)
        return std_so4_pdb,std_so4_hier,std_so4_xrs

    def place_wat(self,b_fac=35.0,occ=1.00):
        #create a dummy pdb, put water
        pdb_string="CRYST1%9.3f%9.3f%9.3f  90.00  90.00  90.00 P 1            1\n" % \
            (2.0*self.bound,2.0*self.bound,2.0*self.bound)
        x1,y1,z1=5.0,5.0,5.0
        pdb_entry = ""
        pdb_entry=pdb_entry+self.write_atom(1,"O","","HOH","X",1,"",x1,y1,z1,occ,b_fac,"O","")
        wat_pdb=pdb_string+pdb_entry
        std_wat_pdb = iotbx.pdb.input(source_info=None, lines=flex.split_lines(wat_pdb))
        local_sym = std_wat_pdb.crystal_symmetry()
        std_wat_hier = std_wat_pdb.construct_hierarchy()
        std_wat_xrs = std_wat_hier.extract_xray_structure(crystal_symmetry = local_sym)
        return std_wat_pdb,std_wat_hier,std_wat_xrs

            
    def gen_null_peak(self):     
        record = self.pput.write_atom(0,"O","","NUL","P",0,"",999.99,999.99,999.99,0.0,30.0,"O","")
        
        tmp_hier = iotbx.pdb.input(source_info=None,lines=flex.split_lines(record))
        pdb_str = tmp_hier.as_pdb_string(crystal_symmetry=self.orig_symmetry)
        null_input=iotbx.pdb.input(source_info=None,lines=flex.split_lines(pdb_str))
        null_hier = null_input.construct_hierarchy()
        return null_hier
