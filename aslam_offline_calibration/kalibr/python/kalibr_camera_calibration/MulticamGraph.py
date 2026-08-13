import sm
import aslam_backend as aopt
import aslam_cv as cv
import kalibr_camera_calibration as kcc

import numpy as np
import collections
import igraph
import itertools
import sys
import pylab as pl
try:
    from PIL import Image # Modern
except ImportError:
    import Image # Old import (backward compatibility)
import time

# make numpy print prettier
np.set_printoptions(suppress=True)


def _snapshotCameraCalibration(camera):
    projection = camera.geometry.projection()
    return (np.array(projection.getParameters(), copy=True),
            np.array(projection.distortion().getParameters(), copy=True))


def _restoreCameraCalibration(camera, snapshot):
    projection = camera.geometry.projection()
    projection.setParameters(snapshot[0])
    projection.distortion().setParameters(snapshot[1])


def _cameraCalibrationIsFinite(camera):
    projection = camera.geometry.projection()
    return (np.all(np.isfinite(np.asarray(projection.getParameters()))) and
            np.all(np.isfinite(np.asarray(projection.distortion().getParameters()))))


def _baselineIsFinite(baseline):
    return baseline is not None and np.all(np.isfinite(np.asarray(baseline.T())))


class MulticamCalibrationGraph(object):
    def __init__(self, obs_db):
        #observation database
        self.obs_db = obs_db
        self.numCams = self.obs_db.numCameras()
        
        #initialize the graph
        self.initializeGraphFromObsDb(self.obs_db)
    
    def initializeGraphFromObsDb(self, obs_db):        
        t0 = time.time()

        #create graph and label the vertices
        G = igraph.Graph(self.numCams)
        for id, vert in enumerate(G.vs):
            vert["label"] = "cam{0}".format(id)
        
        #go through all times
        for timestamp in self.obs_db.getAllViewTimestamps():
            #cameras that have a target view at this timestamp instant
            cam_ids_at_timestamp = set( obs_db.getCamIdsAtTimestamp(timestamp) )
            
            #go through all edges of the graph and check if we have common corners
            possible_edges = itertools.combinations(cam_ids_at_timestamp, 2)
            
            for edge in possible_edges:
                cam_id_A = edge[0]
                cam_id_B = edge[1]
                
                #and check them against the other cams for common corners (except against itself...)
                corners_A = self.obs_db.getCornerIdsAtTime(timestamp, cam_id_A)
                obs_id_A = self.obs_db.getObsIdForCamAtTime(timestamp, cam_id_A)
                corners_B = self.obs_db.getCornerIdsAtTime(timestamp, cam_id_B)
                obs_id_B = self.obs_db.getObsIdForCamAtTime(timestamp, cam_id_B)
                
                common_corners = corners_A & corners_B
                
                #add graph edge if we found common corners
                if common_corners:
                    #add edege if it isn't existing yet
                    try:
                        edge_idx = G.get_eid(cam_id_A, cam_id_B)
                    except:
                        G.add_edges([(cam_id_A, cam_id_B)])
                        edge_idx = G.get_eid(cam_id_A, cam_id_B)
                        G.es[edge_idx]["obs_ids"] = list()
                        G.es[edge_idx]["weight"] = 0
                    
                    #store the observation of the camera if the lower id first on the edge
                    G.es[edge_idx]["weight"] += len(common_corners)
                    G.es[edge_idx]["obs_ids"].append( (obs_id_A, obs_id_B) if cam_id_A<cam_id_B else (obs_id_B, obs_id_A) )
        
        #store the graph  
        self.G = G
        
        #timing
        t1 = time.time()
        total = t1-t0
        sm.logDebug("It took {0}s to build the graph.".format(total))
    
#############################################################
## SYSTEM PROPERTIES
#############################################################    
    #check if all cams are connected through observations
    def isGraphConnected(self):
        if self.numCams == 1:
            # Since igraph 0.8, adhesion correctly returns 0 instead of -2147483648 for a graph with a single vertex.
            # As 0 evaluates to False later in the process, kalibr exits with the cameras unconnected error.
            # So we skip the check and return true in the one camera case.
            # https://github.com/ethz-asl/kalibr/issues/364
            # https://github.com/ethz-asl/kalibr/pull/358
            return True
        else:
            #check if all vertices are connected
            return self.G.adhesion()
        
    #returns the list of cam_ids that share common view with the specified cam_id
    def getCamOverlaps(self, cam_id):
        overlap_vertices=self.G.vs[cam_id].neighbors()
        
        overlaps=list()
        for vert in overlap_vertices:
            overlaps.append(vert.index)
            
        return overlaps
        
#############################################################
## INITIAL GUESS STUFF
#############################################################    
    
    #returns: 
    #        baselines:    list of baselines starting from cam0 to camN
    #                      direction: baseline_O => cam0 to cam1 (T_c1_c0)
    def getInitialGuesses(self, cameras):
        
        if not self.G:
            raise RuntimeError("Graph is uninitialized!")
        
        #################################################################
        ## STEP 0: check if all cameras in the chain are connected
        ##         through common target point observations
        ##         (=all vertices connected?)
        #################################################################
        if not self.isGraphConnected():
            sm.logError("The cameras are not connected through mutual target observations! " 
                        "Please provide another dataset...")
            
            self.plotGraph()
            sys.exit(0)
        
        #################################################################
        ## STEP 1: get baseline initial guesses by calibrating good 
        ##         camera pairs using a stereo calibration
        ## 
        #################################################################

        # Try the strongest co-visibility edges first. Only successful stereo
        # calibrations are committed to the spanning tree; failed attempts are
        # rolled back so another edge can safely reuse either camera.
        candidate_edges = list()
        for edge_id, edge in enumerate(self.G.es):
            camA_nr, camB_nr = edge.tuple
            camL_nr, camH_nr = sorted((camA_nr, camB_nr))
            candidate_edges.append((edge_id, camL_nr, camH_nr, edge["weight"]))
        candidate_edges.sort(key=lambda candidate: (-candidate[3], candidate[1], candidate[2]))

        print("\t co-visibility graph edge candidates:")
        for _edge_id, camL_nr, camH_nr, common_corners in candidate_edges:
            print("\t   cam{0}-cam{1}: common target corners={2}".format(
                camL_nr, camH_nr, common_corners))

        parent = list(range(self.numCams))
        rank = [0] * self.numCams

        def find(camera_id):
            while parent[camera_id] != camera_id:
                parent[camera_id] = parent[parent[camera_id]]
                camera_id = parent[camera_id]
            return camera_id

        def union(camA_nr, camB_nr):
            rootA = find(camA_nr)
            rootB = find(camB_nr)
            if rootA == rootB:
                return
            if rank[rootA] < rank[rootB]:
                rootA, rootB = rootB, rootA
            parent[rootB] = rootA
            if rank[rootA] == rank[rootB]:
                rank[rootA] += 1

        self.optimal_baseline_edges = set()
        successful_edges = list()
        failed_edges = list()

        #################################################################
        ## STEP 2: solve stereo calibration problem for the baselines
        ##         (baselines are always from lower_id to higher_id cams!)
        #################################################################
        for baseline_edge_id, camL_nr, camH_nr, common_corners in candidate_edges:
            if find(camL_nr) == find(camH_nr):
                continue

            print("\t initializing camera pair ({0},{1})...  ".format(camL_nr, camH_nr))
            try:
                snapshots = (_snapshotCameraCalibration(cameras[camL_nr]),
                             _snapshotCameraCalibration(cameras[camH_nr]))
            except Exception as exc:
                reason = "parameter snapshot failed: {0}: {1}".format(type(exc).__name__, exc)
                failed_edges.append((camL_nr, camH_nr, common_corners, reason))
                sm.logWarn("camera pair ({0},{1}) skipped: {2}".format(
                    camL_nr, camH_nr, reason))
                continue

            reason = None
            baseline_HL = None
            try:
                obs_list = self.obs_db.getAllObsTwoCams(camL_nr, camH_nr)
                success, baseline_HL = kcc.stereoCalibrate(
                    cameras[camL_nr], cameras[camH_nr], obs_list,
                    distortionActive=False)
                if not success:
                    reason = "stereoCalibrate returned success=False"
                elif not _baselineIsFinite(baseline_HL):
                    reason = "stereoCalibrate returned a non-finite baseline"
                elif not _cameraCalibrationIsFinite(cameras[camL_nr]):
                    reason = "cam{0} has non-finite calibration parameters".format(camL_nr)
                elif not _cameraCalibrationIsFinite(cameras[camH_nr]):
                    reason = "cam{0} has non-finite calibration parameters".format(camH_nr)
            except Exception as exc:
                reason = "{0}: {1}".format(type(exc).__name__, exc)

            if reason is not None:
                try:
                    _restoreCameraCalibration(cameras[camL_nr], snapshots[0])
                    _restoreCameraCalibration(cameras[camH_nr], snapshots[1])
                except Exception as exc:
                    raise RuntimeError(
                        "Failed to restore camera parameters after stereo initialization "
                        "failure for cam{0}-cam{1}: {2}: {3}".format(
                            camL_nr, camH_nr, type(exc).__name__, exc))
                failed_edges.append((camL_nr, camH_nr, common_corners, reason))
                sm.logWarn("camera pair ({0},{1}) failed, trying another edge: {2}".format(
                    camL_nr, camH_nr, reason))
                continue

            self.G.es[baseline_edge_id]["baseline_HL"] = baseline_HL
            self.optimal_baseline_edges.add(baseline_edge_id)
            successful_edges.append((camL_nr, camH_nr, common_corners))
            union(camL_nr, camH_nr)
            sm.logDebug("baseline_{0}_{1}={2}".format(camL_nr, camH_nr, baseline_HL.T()))

            if len(self.optimal_baseline_edges) == self.numCams - 1:
                break

        if len(self.optimal_baseline_edges) != self.numCams - 1:
            components_by_root = dict()
            for camera_id in range(self.numCams):
                components_by_root.setdefault(find(camera_id), list()).append(camera_id)
            components = sorted(components_by_root.values(), key=lambda component: component[0])
            successful_summary = [
                "cam{0}-cam{1}(corners={2})".format(*edge)
                for edge in successful_edges
            ]
            failed_summary = [
                "cam{0}-cam{1}(corners={2}, reason={3})".format(*edge)
                for edge in failed_edges
            ]
            raise RuntimeError(
                "Stereo initialization could not connect all cameras; successful edges: {0}; "
                "failed edges: {1}; components: {2}".format(
                    successful_summary, failed_summary, components))

        print("\t selected successful baseline init edges: {0}".format(
            [self.G.es[edge_id].tuple for edge_id in sorted(self.optimal_baseline_edges)]))
        
        #################################################################
        ## STEP 3: transform from the "optimal" baseline chain to camera chain ordering
        ##         (=> baseline_0 = T_c1_c0 | 
        #################################################################
        
        #construct the optimal path graph
        G_optimal_baselines = self.G.copy()
        
        eid_not_optimal_path = set(range(0,len(G_optimal_baselines.es)))
        for eid in self.optimal_baseline_edges:
            eid_not_optimal_path.remove(eid)
        G_optimal_baselines.delete_edges( eid_not_optimal_path )
        
        #now we convert the arbitary baseline graph to baselines starting from 
        # cam0 and traverse the chain (cam0->cam1->cam2->camN)
        baselines = []
        for baseline_id in range(0, self.numCams-1):
            #find the shortest path on the graph
            path = G_optimal_baselines.get_shortest_paths(baseline_id, baseline_id+1)[0]
            
            #get the baseline from cam with id baseline_id to baseline_id+1
            baseline_HL = sm.Transformation()
            for path_idx in range(0, len(path)-1):
                source_vert = path[path_idx]
                target_vert = path[path_idx+1]
                T_edge = self.G.es[ self.G.get_eid(source_vert, target_vert) ]["baseline_HL"]
            
                #correct the direction (baselines always from low to high cam id!)
                T_edge = T_edge if source_vert<target_vert else T_edge.inverse()
            
                #chain up
                baseline_HL = T_edge * baseline_HL
            
            #store in graph
            baselines.append(baseline_HL)
 
        #################################################################
        ## STEP 4: refine guess in full batch
        #################################################################
        success, baselines = kcc.solveFullBatch(cameras, baselines, self)
        
        if not success:
            sm.logWarn("Full batch refinement failed!")
    
        return baselines
    
    def getTargetPoseGuess(self, timestamp, cameras, baselines_HL=[]):
        #go through all camera that see this target at the given time
        #and take the one with the most target points        
        camids = list()
        numcorners = list()
        for cam_id in self.obs_db.getCamIdsAtTimestamp(timestamp):
            camids.append(cam_id)
            numcorners.append( len(self.obs_db.getCornerIdsAtTime(timestamp, cam_id)) )

        #get the pnp solution of the cam that sees most target corners
        max_idx = numcorners.index(max(numcorners))
        cam_id_max = camids[max_idx]        
        
        #solve the pnp problem
        camera_geomtry = cameras[cam_id_max].geometry
        success, T_t_cN = camera_geomtry.estimateTransformation(self.obs_db.getObservationAtTime(timestamp, cam_id_max))
               
        if not success:
            sm.logWarn("getTargetPoseGuess: solvePnP failed with solution: {0}".format(T_t_cN))
            return None
        
        #transform it back to cam0 (T_t_cN --> T_t_c0)
        T_cN_c0 = sm.Transformation()
        for baseline_HL in baselines_HL[0:cam_id_max]:
            T_cN_c0 = baseline_HL * T_cN_c0
        
        T_t_c0 = T_t_cN * T_cN_c0
        
        return T_t_c0
    
    #get all observations between two cameras
    def getAllMutualObsBetweenTwoCams(self, camA_nr, camB_nr):
        #get the observation ids
        try:
            edge_idx = self.G.get_eid(camA_nr, camB_nr)
        except:
            sm.logError("getAllMutualObsBetweenTwoCams: no mutual observations between the two cams!")
            return [], []
        
        observations = self.G.es[edge_idx]["obs_ids"]
        
        #extract the ids
        obs_idx_L = [obs_ids[0] for obs_ids in observations]
        obs_idx_H = [obs_ids[1] for obs_ids in observations]
        
        #the first value of the tuple always stores the obsvervations for camera
        #with the lower id
        obs_idx_A = obs_idx_L if camA_nr<camB_nr else obs_idx_H
        obs_idx_B = obs_idx_H if camA_nr<camB_nr else obs_idx_L
        
        #get the obs from the storage using idx
        obs_A = [self.obs_db.observations[camA_nr][idx] for idx in obs_idx_A]
        obs_B = [self.obs_db.observations[camB_nr][idx] for idx in obs_idx_B]

        return obs_A, obs_B
         
#############################################################
## PLOTTING AND PRINTING
#############################################################            
    def plotGraph(self, noShow=False):        
        layout = self.G.layout("kk")
        
        #plot the optimal baselines for pair calibration
        try:
            edgewidth=[]
            for edge_idx, edge in enumerate(self.G.es):
                if edge_idx in self.optimal_baseline_edges:
                    edgewidth.append(10)
                else:
                    edgewidth.append(2)
        except AttributeError:
            edgewidth = 2
        
        if not noShow:
            target = None
        else:
            target = "/tmp/graph.png"

        plot = igraph.plot(self.G, 
                           layout=layout, 
                           rescale=False, 
                           add=False,
                           target=target,
                           vertex_size=50,
                           edge_label=self.G.es["weight"],
                           edge_width=edgewidth,
                           margin = 50)
        
        if not noShow:
            return plot
        else:
            return target

    def plotGraphPylab(self, fno=0, noShow=True, clearFigure=True, title=""):
        target = self.plotGraph(noShow=True)
        
        #create figure
        f = pl.figure(fno)
        if clearFigure:    
            f.clf()
        f.suptitle(title)
        
        img = Image.open(target)
        pl.imshow(np.array(img))
        pl.axis('off')
        
        if not noShow:
            pl.show()
