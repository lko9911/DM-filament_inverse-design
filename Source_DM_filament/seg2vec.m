function V = seg2vec(Seg, ds, interfdist)
% vec = [material num, startpoint, endpoint, line num, layer num]

V = Seg(1,:);
ii = 1;
% connect vectors
for i = 1:size(Seg,1)-1
    curID = [V(ii,1), V(ii,3:6)];
    nextID = [Seg(i+1,1:2), Seg(i+1,4:6)];
    if isequal(curID, nextID)         
        V(ii,3) = Seg(i+1,3);        
%         i=i+1;
    else
        ii = ii+1;   
        V(ii,:) = Seg(i+1,:);
    end    
end
V( V(:,1)==0 , :) = [];

d = interfdist/ds; % +- moving dist to avoid collision between sections
Lmax = max(V(:,3));
for j = 1:size(V,1)
    if V(j,2) ~= 1
        V(j,2) = V(j,2) + d ;
    end
    if V(j,3) ~= Lmax
        V(j,3) = V(j,3) - d;
    end
end

    
% 
% global lineN layerN Linfo
% 
% interval = [1; unique(Seg(:,3)) ];
% intN = size(interval,1)-1;
% interf = cell(intN,1);
% interfDist = 2;
% 
% % generate interface matrix of each segments
% for j = 1:intN
%     interf{j,1} = [ repmat( [0, interval(j),interval(j+1)],lineN,1 ) (1:1:lineN)' (1:1:lineN)'];    
%     interf{j,1}(:,4) = line2layer(interf{j,1}(:,4));
% end
% 
% % assign material(based on printing order) to each segments
% for i = 1:size(Seg,1)
%     p = find( Seg(i,2) == interval ); 
%     interf{ p,1 }( Seg(i,4):Seg(i,5) ,1 ) = Seg(i,1);    
%     % interf = [material num, startpoint, endpoint, layer num, line num]
% end
% 
% % modify interfaces to be oblique to avoid collision between nozzle and previously printed segment
% 
% % move = zeros(lineN,1);             % move distance in point-dir to generate transition interval
% dir = zeros(layerN,1);              % direction of move according to printing order
% transition = zeros(intN-1,3);   % memorize transition interval
% 
% for i = 1:intN-1
%     for curLayer = 1:layerN
%         La = find(interf{i,1}(:,4) == curLayer); 
%         curM = find( po == min(interf{i,1}(La,1)) );
%         nextM = find( po == min(interf{i+1,1}(La,1)) );
%         if nextM < curM
%             dir(curLayer) = 1;
%         elseif nextM > curM
%             dir(curLayer) = -1;
%         end    
%     end
%     c = find(interf{i+1,1}(:,1)~=interf{i,1}(:,1)); 
% 
%     if sum(c) ~= 0
%         move = -line2layer(c(1)) + (1:1:layerN)';
%     else
%         move = zeros(layerN,1);
%     end
%     move = move.*dir*stitch;
%     cc = find(move<0);
%     if sum(cc) ~= 0
%         move = move + abs(min(move));
%     end
% 
%     b = cumsum(transition(:,3));
%     for curLine = 1:lineN      
%         m = line2layer(curLine,Linfo);
%         curMove = move(m);
%         interf{i,1}(curLine,3) = interf{i,1}(curLine,3) + curMove + b(end);
%         interf{i+1,1}(curLine,2) = interf{i+1,1}(curLine,2) + curMove + b(end) + interfDist;
%         transition(i,1:2) = [min(interf{i+1,1}(curLine,2)), max(interf{i+1,1}(curLine,2))];
%         transition(i,3) = transition(i,2)-transition(i,1);
%     end
% end
% interf{end,1}(:,3) = interf{end,1}(:,3) + b(end);
% 
% % rearrange {interf}
% c=1;
% vec = zeros(1,5);
% for i=1:length(po)  
%     for j=1:lineN  
%         for k=1:intN                     
%             if po(i) == interf{k,1}(j,1)                
%                 vec(c,:) = interf{k,1}(j,:);
%                 c = c+1;
%             end                            
%         end
%     end
% end
% 
% vtemp = zeros(size(Seg));
% ii = 1;
% % connect vectors
% for i=1:size(vec,1)-1
%     if vec(i+1,2)- vec(i,3) ~= interfDist  
%         vtemp(ii,:) = [vec(ii,1:2), vec(i,3:5)];
%         ii = i+1;
%     end
% end
% vtemp(ii,:) = [vec(ii,1:2), vec(i+1,3:5)];
% b = find(vtemp(:,1)==0);
% vtemp(b,:) = [];
% 
% vtemp = sortrows(vtemp, [4 2 3]);
% 
% % rearrange vectors in layer order
% Vs = zeros(1,6);
% ci=1;
% for i=1:size(vtemp,1)-1
%     if vtemp(i+1,1) ~= vtemp(ci,1) || vtemp(i+1,2) ~= vtemp(ci,2) || vtemp(i+1,3) ~= vtemp(ci,3) || vtemp(i+1,4) ~= vtemp(ci,4)
%         Vs(ci,:) = [vtemp(ci,1:5), vtemp(i,5)];
%         ci=i+1;
%     end
% end
% Vs(ci,:) = [vtemp(ci,1:5), vtemp(i+1,5)];
% b = find(Vs(:,1)==0);
% Vs(b,:) = [];    
% 
% 
% % rearrange V in PO order
% vi = 1;
% V = zeros(size(Vs));
% for i = 1:size(po,1)
%     mi = find(Vs(:,1) == po(i));    
%     vo = vi+size(mi,1)-1;
%     V(vi:vo,:) = Vs(mi,:);
%     vi = vo+1;     
% end

end

